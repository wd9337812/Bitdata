from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.binance_client import BinanceFuturesClient
from app.live_learning import (
    build_trade_records_from_user_trades,
    init_live_learning_schema,
    rebuild_symbol_scores,
    upsert_trade_records,
)
from app.telemetry import connect, now_iso, record_event, record_event_throttled


def init_live_reaction_schema() -> None:
    init_live_learning_schema()
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS symbol_reaction_state (
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                status TEXT NOT NULL,
                status_label TEXT NOT NULL,
                risk_multiplier REAL NOT NULL,
                score_penalty REAL NOT NULL,
                consecutive_wins INTEGER NOT NULL,
                consecutive_losses INTEGER NOT NULL,
                recent_net_pnl REAL NOT NULL,
                day_net_pnl REAL NOT NULL,
                closed_trades INTEGER NOT NULL,
                cooldown_until TEXT,
                ban_until TEXT,
                last_trade_time INTEGER,
                reason TEXT,
                payload TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(symbol, direction)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_symbol_reaction_status ON symbol_reaction_state(status, updated_at)")
        conn.commit()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso_from_ms(ms: int | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _label(status: str) -> str:
    return {
        "normal": "正常",
        "cooldown": "降仓观察",
        "tail_guard": "盈利防追尾",
        "banned": "暂停同向",
    }.get(status, status)


def _until_from_close(record: dict[str, Any], **kwargs: float) -> datetime:
    close_ms = int(record.get("close_time") or time.time() * 1000)
    return datetime.fromtimestamp(close_ms / 1000, timezone.utc) + timedelta(**kwargs)


def _previous_win_streak_before_last(records: list[dict[str, Any]]) -> int:
    if len(records) < 2:
        return 0
    streak = 0
    for record in reversed(records[:-1]):
        if float(record.get("net_pnl") or 0) > 0:
            streak += 1
        else:
            break
    return streak


def _day_damage_metrics(
    records: list[dict[str, Any]],
    day_start_ms: int,
    equity_base: float,
    config: dict[str, Any],
) -> dict[str, Any]:
    day_records = [item for item in records if int(item.get("close_time") or 0) >= day_start_ms]
    cumulative = 0.0
    peak_profit = 0.0
    largest_loss = 0.0
    largest_loss_pct = 0.0
    for item in day_records:
        net = float(item.get("net_pnl") or 0)
        cumulative += net
        if cumulative > peak_profit:
            peak_profit = cumulative
        if net < largest_loss:
            largest_loss = net
            largest_loss_pct = abs(net) / equity_base * 100
    giveback = max(0.0, peak_profit - cumulative) if peak_profit > 0 else 0.0
    min_profit = float(config.get("live_reaction_giveback_min_profit_usdt", 1.0))
    giveback_pct = giveback / peak_profit * 100 if peak_profit >= min_profit and peak_profit > 0 else 0.0
    return {
        "day_records": day_records,
        "largest_loss": round(largest_loss, 8),
        "largest_loss_pct": round(largest_loss_pct, 4),
        "peak_profit": round(peak_profit, 8),
        "giveback": round(giveback, 8),
        "giveback_pct": round(giveback_pct, 4),
    }


def score_reaction_records(
    records: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    equity: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    records = sorted(records, key=lambda item: int(item.get("close_time") or 0))
    if not records:
        return {
            "status": "normal",
            "status_label": _label("normal"),
            "risk_multiplier": 1.0,
            "score_penalty": 0.0,
            "consecutive_wins": 0,
            "consecutive_losses": 0,
            "recent_net_pnl": 0.0,
            "day_net_pnl": 0.0,
            "closed_trades": 0,
            "cooldown_until": None,
            "ban_until": None,
            "last_trade_time": None,
            "reason": "暂无近期实盘记录",
        }

    consecutive_wins = 0
    consecutive_losses = 0
    for record in reversed(records):
        net = float(record.get("net_pnl") or 0)
        if net > 0 and consecutive_losses == 0:
            consecutive_wins += 1
            continue
        if net <= 0 and consecutive_wins == 0:
            consecutive_losses += 1
            continue
        break

    recent_window_minutes = float(config.get("live_reaction_recent_loss_window_minutes", 10))
    recent_cutoff = _ms(now - timedelta(minutes=recent_window_minutes))
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    recent_records = [item for item in records if int(item.get("close_time") or 0) >= recent_cutoff]
    day_records = [item for item in records if int(item.get("close_time") or 0) >= _ms(day_start)]
    recent_net = sum(float(item.get("net_pnl") or 0) for item in recent_records)
    day_net = sum(float(item.get("net_pnl") or 0) for item in day_records)
    last = records[-1]
    last_close = int(last.get("close_time") or 0)
    reason_parts: list[str] = []
    status = "normal"
    risk_multiplier = 1.0
    score_penalty = 0.0
    cooldown_until: datetime | None = None
    ban_until: datetime | None = None

    if consecutive_losses >= 3:
        status = "banned"
        risk_multiplier = 0.0
        score_penalty = 35.0
        ban_until = _until_from_close(last, hours=float(config.get("live_reaction_three_loss_ban_hours", 4)))
        reason_parts.append(f"同方向三连亏，禁开到 {ban_until.isoformat()}")
    elif consecutive_losses >= 2:
        status = "banned"
        risk_multiplier = 0.0
        score_penalty = 24.0
        ban_until = _until_from_close(last, minutes=float(config.get("live_reaction_two_loss_ban_minutes", 30)))
        reason_parts.append(f"同方向两连亏，禁开到 {ban_until.isoformat()}")
    elif consecutive_losses >= 1:
        status = "cooldown"
        risk_multiplier = min(risk_multiplier, float(config.get("live_reaction_one_loss_multiplier", 0.4)))
        score_penalty = max(score_penalty, float(config.get("live_reaction_score_penalty", 8.0)))
        cooldown_until = _until_from_close(last, minutes=float(config.get("live_reaction_one_loss_cooldown_minutes", 5)))
        reason_parts.append(f"上一笔亏损，降仓到 {risk_multiplier:.2f}x")

    equity_base = max(float(equity or 0), 0.0001)
    damage = _day_damage_metrics(records, _ms(day_start), equity_base, config)
    single_loss_pct = float(damage["largest_loss_pct"])
    if single_loss_pct >= float(config.get("live_reaction_single_loss_ban_equity_pct", 10.0)):
        until = _until_from_close(last, minutes=float(config.get("live_reaction_single_loss_ban_minutes", 180)))
        if ban_until is None or until > ban_until:
            ban_until = until
        status = "banned"
        risk_multiplier = 0.0
        score_penalty = max(score_penalty, 34.0)
        reason_parts.append(f"当日最大单笔亏损 {single_loss_pct:.1f}% 权益，暂停同向")
    elif single_loss_pct >= float(config.get("live_reaction_single_loss_cooldown_equity_pct", 6.0)):
        until = _until_from_close(last, minutes=float(config.get("live_reaction_single_loss_cooldown_minutes", 60)))
        if cooldown_until is None or until > cooldown_until:
            cooldown_until = until
        if status != "banned":
            status = "cooldown"
            risk_multiplier = min(risk_multiplier, float(config.get("live_reaction_one_loss_multiplier", 0.4)))
        score_penalty = max(score_penalty, 18.0)
        reason_parts.append(f"当日最大单笔亏损 {single_loss_pct:.1f}% 权益，延长降仓观察")

    recent_loss_pct = abs(recent_net) / equity_base * 100 if recent_net < 0 else 0.0
    if recent_loss_pct >= float(config.get("live_reaction_recent_loss_equity_pct", 12.0)):
        until = _until_from_close(last, minutes=float(config.get("live_reaction_recent_loss_ban_minutes", 120)))
        if ban_until is None or until > ban_until:
            ban_until = until
        status = "banned"
        risk_multiplier = 0.0
        score_penalty = max(score_penalty, 30.0)
        reason_parts.append(f"{recent_window_minutes:.0f} 分钟净亏 {recent_loss_pct:.1f}% 权益，暂停同向")

    day_loss_pct = abs(day_net) / equity_base * 100 if day_net < 0 else 0.0
    if day_loss_pct >= float(config.get("live_reaction_symbol_direction_daily_loss_pct", 15.0)):
        until = day_start + timedelta(days=1)
        if ban_until is None or until > ban_until:
            ban_until = until
        status = "banned"
        risk_multiplier = 0.0
        score_penalty = max(score_penalty, 40.0)
        reason_parts.append(f"当日同向净亏 {day_loss_pct:.1f}% 权益，暂停到次日")

    giveback_pct = float(damage["giveback_pct"])
    if giveback_pct >= float(config.get("live_reaction_giveback_ban_pct", 80.0)):
        until = _until_from_close(last, minutes=float(config.get("live_reaction_giveback_ban_minutes", 180)))
        if ban_until is None or until > ban_until:
            ban_until = until
        status = "banned"
        risk_multiplier = 0.0
        score_penalty = max(score_penalty, 36.0)
        reason_parts.append(
            f"当日盈利从 {damage['peak_profit']:.2f}U 回吐 {giveback_pct:.1f}%，暂停同向"
        )
    elif giveback_pct >= float(config.get("live_reaction_giveback_cooldown_pct", 50.0)):
        until = _until_from_close(last, minutes=float(config.get("live_reaction_giveback_cooldown_minutes", 60)))
        if cooldown_until is None or until > cooldown_until:
            cooldown_until = until
        if status != "banned":
            status = "tail_guard"
            risk_multiplier = min(risk_multiplier, float(config.get("live_reaction_tail_multiplier", 0.5)))
        score_penalty = max(score_penalty, 20.0)
        reason_parts.append(
            f"当日盈利从 {damage['peak_profit']:.2f}U 回吐 {giveback_pct:.1f}%，防追尾降仓"
        )

    tail_count = int(config.get("live_reaction_profit_tail_count", 2))
    if consecutive_wins >= tail_count and status != "banned":
        status = "tail_guard"
        risk_multiplier = min(risk_multiplier, float(config.get("live_reaction_tail_multiplier", 0.5)))
        score_penalty = max(score_penalty, float(config.get("live_reaction_tail_score_penalty", 5.0)))
        cooldown_until = _until_from_close(last, minutes=float(config.get("live_reaction_tail_cooldown_minutes", 15)))
        reason_parts.append(f"连续盈利 {consecutive_wins} 笔，防追尾降仓到 {risk_multiplier:.2f}x")

    if float(last.get("net_pnl") or 0) <= 0 and _previous_win_streak_before_last(records) >= tail_count:
        until = _until_from_close(last, minutes=float(config.get("live_reaction_tail_loss_cooldown_minutes", 30)))
        if ban_until is None or until > ban_until:
            ban_until = until
        status = "banned"
        risk_multiplier = 0.0
        score_penalty = max(score_penalty, 28.0)
        reason_parts.append("连续盈利后追尾亏损，暂停同向")

    if ban_until and ban_until <= now:
        ban_until = None
        if status == "banned":
            status = "normal"
            risk_multiplier = 1.0
            score_penalty = 0.0
            reason_parts.append("禁开已过期")
    if cooldown_until and cooldown_until <= now:
        cooldown_until = None
        if status in {"cooldown", "tail_guard"}:
            status = "normal"
            risk_multiplier = 1.0
            score_penalty = 0.0
            reason_parts.append("降仓观察已过期")

    if not reason_parts:
        reason_parts.append("近期实盘未触发快刹车")

    return {
        "status": status,
        "status_label": _label(status),
        "risk_multiplier": round(max(0.0, min(1.0, risk_multiplier)), 4),
        "score_penalty": round(score_penalty, 4),
        "consecutive_wins": consecutive_wins,
        "consecutive_losses": consecutive_losses,
        "recent_net_pnl": round(recent_net, 8),
        "day_net_pnl": round(day_net, 8),
        "closed_trades": len(records),
        "cooldown_until": cooldown_until.isoformat() if cooldown_until else None,
        "ban_until": ban_until.isoformat() if ban_until else None,
        "last_trade_time": last_close,
        "last_trade_time_iso": _iso_from_ms(last_close),
        "reason": "；".join(reason_parts),
        "payload": {
            "recent_window_minutes": recent_window_minutes,
            "recent_loss_pct": round(recent_loss_pct, 4),
            "day_loss_pct": round(day_loss_pct, 4),
            "last_net_pnl": round(float(last.get("net_pnl") or 0), 8),
            "largest_single_loss_pct": single_loss_pct,
            "largest_single_loss": damage["largest_loss"],
            "day_peak_profit": damage["peak_profit"],
            "day_profit_giveback": damage["giveback"],
            "day_profit_giveback_pct": giveback_pct,
        },
    }


def rebuild_live_reaction_state(
    config: dict[str, Any],
    *,
    lookback_hours: float | None = None,
    equity: float | None = None,
) -> list[dict[str, Any]]:
    init_live_reaction_schema()
    lookback_hours = lookback_hours or float(config.get("live_reaction_history_hours", config.get("live_credit_history_hours", 96)))
    cutoff_ms = int((time.time() - lookback_hours * 3600) * 1000)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM live_trade_records WHERE close_time >= ? ORDER BY close_time ASC",
            (cutoff_ms,),
        ).fetchall()
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            item = dict(row)
            grouped.setdefault((str(item["symbol"]).upper(), str(item["direction"]).upper()), []).append(item)
        results = []
        for (symbol, direction), records in grouped.items():
            state = score_reaction_records(records, config, equity=equity)
            conn.execute(
                """
                INSERT OR REPLACE INTO symbol_reaction_state (
                    symbol, direction, status, status_label, risk_multiplier, score_penalty,
                    consecutive_wins, consecutive_losses, recent_net_pnl, day_net_pnl,
                    closed_trades, cooldown_until, ban_until, last_trade_time, reason,
                    payload, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    direction,
                    state["status"],
                    state["status_label"],
                    state["risk_multiplier"],
                    state["score_penalty"],
                    state["consecutive_wins"],
                    state["consecutive_losses"],
                    state["recent_net_pnl"],
                    state["day_net_pnl"],
                    state["closed_trades"],
                    state["cooldown_until"],
                    state["ban_until"],
                    state["last_trade_time"],
                    state["reason"],
                    json.dumps(state.get("payload") or {}, ensure_ascii=False),
                    now_iso(),
                ),
            )
            results.append({"symbol": symbol, "direction": direction, **state})
        conn.commit()
    return sorted(results, key=lambda item: (item.get("status") == "banned", item.get("last_trade_time") or 0), reverse=True)


def live_reaction_for(symbol: str, direction: str, config: dict[str, Any]) -> dict[str, Any]:
    if not config.get("live_reaction_enabled", True):
        return {"enabled": False, "status": "disabled", "status_label": "未启用", "risk_multiplier": 1.0}
    init_live_reaction_schema()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM symbol_reaction_state WHERE symbol = ? AND direction = ?",
            (symbol.upper(), direction.upper()),
        ).fetchone()
    if not row:
        return {
            "enabled": True,
            "symbol": symbol.upper(),
            "direction": direction.upper(),
            "status": "normal",
            "status_label": "新方向",
            "risk_multiplier": 1.0,
            "score_penalty": 0.0,
            "reason": "暂无近期实盘反应记录",
        }
    item = dict(row)
    try:
        item["payload"] = json.loads(item.get("payload") or "{}")
    except json.JSONDecodeError:
        item["payload"] = {}
    item["enabled"] = True
    item["last_trade_time_iso"] = _iso_from_ms(item.get("last_trade_time"))
    now = datetime.now(timezone.utc)
    ban_until = _parse_dt(item.get("ban_until"))
    cooldown_until = _parse_dt(item.get("cooldown_until"))
    item["ban_active"] = bool(ban_until and ban_until > now)
    item["cooldown_active"] = bool(cooldown_until and cooldown_until > now)
    if item["status"] == "banned" and not item["ban_active"]:
        item["status"] = "normal"
        item["status_label"] = _label("normal")
        item["risk_multiplier"] = 1.0
        item["score_penalty"] = 0.0
        item["reason"] = "实时禁开已过期"
    if item["status"] in {"cooldown", "tail_guard"} and not item["cooldown_active"]:
        item["status"] = "normal"
        item["status_label"] = _label("normal")
        item["risk_multiplier"] = 1.0
        item["score_penalty"] = 0.0
        item["reason"] = "实时降仓已过期"
    return item


def apply_live_reaction_to_candidate(candidate: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    if not config.get("live_reaction_enabled", True):
        return candidate
    if str(candidate.get("strategy_family") or "") == "extreme_v4_roll":
        result = dict(candidate)
        result["live_reaction"] = {
            "enabled": False,
            "status": "strategy_isolated",
            "status_label": "V4 独立证据",
            "risk_multiplier": 1.0,
            "reason": "旧策略的币种方向快反记录不参与 V4；V4 使用独立信用与版本证据",
        }
        return result
    symbol = str(candidate.get("symbol") or "").upper()
    direction = str(candidate.get("direction") or "").upper()
    if not symbol or direction not in {"LONG", "SHORT"}:
        return candidate
    reaction = live_reaction_for(symbol, direction, config)
    candidate = dict(candidate)
    candidate["live_reaction"] = reaction
    multiplier = float(reaction.get("risk_multiplier") or 0)
    score_penalty = float(reaction.get("score_penalty") or 0)
    status = str(reaction.get("status") or "")
    if score_penalty:
        candidate["score"] = round(float(candidate.get("score") or 0) - score_penalty, 4)
    if status == "banned" and reaction.get("ban_active", True):
        candidate["passed"] = False
        candidate["reason"] = "live_reaction_ban"
        candidate["decision_reason"] = f"实时风控暂停同向：{reaction.get('reason')}"
    elif multiplier < 1.0:
        candidate["risk_pct"] = round(float(candidate.get("risk_pct") or 0) * multiplier, 8)
        suffix = f"实时风控 {reaction.get('status_label')}，仓位倍率 {multiplier:.2f}x：{reaction.get('reason')}"
        candidate["decision_reason"] = f"{candidate.get('decision_reason') or candidate.get('reason') or ''}；{suffix}".strip("；")
    candidate["live_reaction_adjustment"] = {
        "status": status,
        "risk_multiplier": round(multiplier, 4),
        "score_penalty": round(score_penalty, 4),
        "reason": reaction.get("reason"),
        "cooldown_until": reaction.get("cooldown_until"),
        "ban_until": reaction.get("ban_until"),
    }
    return candidate


def _recent_strategy_symbols(limit: int, minutes: int) -> list[str]:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT symbol FROM strategy_runs
            WHERE symbol IS NOT NULL AND ts >= ?
            ORDER BY id DESC LIMIT ?
            """,
            (cutoff, limit * 3),
        ).fetchall()
    symbols: list[str] = []
    for row in rows:
        symbol = str(row["symbol"] or "").upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
        if len(symbols) >= limit:
            break
    return symbols


def _position_symbols(account: dict[str, Any] | None) -> list[str]:
    symbols: list[str] = []
    for position in (account or {}).get("positions", []) or []:
        try:
            if abs(float(position.get("positionAmt", 0) or 0)) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        symbol = str(position.get("symbol") or "").upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols


def sync_live_reaction_from_binance(
    client: BinanceFuturesClient,
    config: dict[str, Any],
    *,
    account: dict[str, Any] | None = None,
    symbols: list[str] | None = None,
    equity: float | None = None,
) -> dict[str, Any]:
    if config.get("dry_run", True) or not config.get("live_reaction_enabled", True):
        return {"enabled": False, "reason": "disabled_or_dry_run"}
    lookback_minutes = int(config.get("live_reaction_sync_lookback_minutes", 180))
    max_symbols = int(config.get("live_reaction_sync_max_symbols", 8))
    end_ms = int(time.time() * 1000)
    start_ms = int((time.time() - lookback_minutes * 60) * 1000)
    income = client.signed_request("GET", "/fapi/v1/income", {"startTime": start_ms, "endTime": end_ms, "limit": 1000})
    symbol_pool: list[str] = []
    for symbol in list(symbols or []) + _position_symbols(account) + _recent_strategy_symbols(max_symbols, lookback_minutes):
        symbol = str(symbol or "").upper()
        if symbol and symbol not in symbol_pool:
            symbol_pool.append(symbol)
    for row in income:
        symbol = str(row.get("symbol") or "").upper()
        if symbol and symbol not in symbol_pool:
            symbol_pool.append(symbol)
    symbol_pool = symbol_pool[:max_symbols]
    trades_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for symbol in symbol_pool:
        rows = client.signed_request(
            "GET",
            "/fapi/v1/userTrades",
            {"symbol": symbol, "startTime": start_ms, "endTime": end_ms, "limit": 1000},
        )
        if rows:
            trades_by_symbol[symbol] = rows
    records = build_trade_records_from_user_trades(trades_by_symbol, income)
    upserted = upsert_trade_records(records)
    scores = rebuild_symbol_scores(config, lookback_hours=max(1.0, lookback_minutes / 60))
    reactions = rebuild_live_reaction_state(
        config,
        lookback_hours=max(1.0, lookback_minutes / 60),
        equity=equity,
    )
    result = {
        "enabled": True,
        "lookback_minutes": lookback_minutes,
        "symbols": symbol_pool,
        "records": upserted,
        "scores": scores,
        "reactions": reactions,
    }
    record_event_throttled(
        "info",
        "live_reaction",
        "实时风控已同步",
        {"symbols": symbol_pool, "records": upserted, "active": [item for item in reactions if item.get("status") != "normal"][:8]},
        throttle_seconds=60,
    )
    return result


def list_live_reactions(limit: int = 100) -> list[dict[str, Any]]:
    init_live_reaction_schema()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM symbol_reaction_state
            ORDER BY
              CASE status WHEN 'banned' THEN 0 WHEN 'cooldown' THEN 1 WHEN 'tail_guard' THEN 2 ELSE 3 END,
              last_trade_time DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [live_reaction_for(str(row["symbol"]), str(row["direction"]), {"live_reaction_enabled": True}) for row in rows]


def list_recent_live_reaction_trades(limit: int = 50) -> list[dict[str, Any]]:
    init_live_reaction_schema()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM live_trade_records ORDER BY close_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        item["open_time_iso"] = _iso_from_ms(item.get("open_time"))
        item["close_time_iso"] = _iso_from_ms(item.get("close_time"))
        try:
            item["payload"] = json.loads(item.get("payload") or "{}")
        except json.JSONDecodeError:
            item["payload"] = {}
        results.append(item)
    return results
