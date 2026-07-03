from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.binance_client import BinanceFuturesClient
from app.telemetry import connect, now_iso, record_event


DEFAULT_SCORE = 50.0


def init_live_learning_schema() -> None:
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS live_trade_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                open_time INTEGER NOT NULL,
                close_time INTEGER NOT NULL,
                open_price REAL,
                close_price REAL,
                quantity REAL,
                open_notional REAL,
                close_notional REAL,
                realized_pnl REAL,
                commission REAL,
                funding_fee REAL DEFAULT 0,
                net_pnl REAL,
                hold_seconds REAL,
                trade_count INTEGER,
                source TEXT,
                payload TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(symbol, direction, open_time, close_time)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS symbol_live_scores (
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                score REAL NOT NULL,
                status TEXT NOT NULL,
                closed_trades INTEGER NOT NULL,
                wins INTEGER NOT NULL,
                losses INTEGER NOT NULL,
                win_rate REAL NOT NULL,
                net_pnl REAL NOT NULL,
                commission REAL NOT NULL,
                funding_fee REAL NOT NULL,
                profit_factor REAL NOT NULL,
                consecutive_wins INTEGER NOT NULL,
                consecutive_losses INTEGER NOT NULL,
                avg_hold_seconds REAL NOT NULL,
                penalty_until TEXT,
                last_trade_time INTEGER,
                notes TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(symbol, direction)
            )
            """
        )
        conn.commit()


def _status_for_score(score: float) -> str:
    if score >= 80:
        return "strong"
    if score >= 65:
        return "normal"
    if score >= 45:
        return "observe"
    if score >= 30:
        return "weak"
    return "penalty"


def status_label(status: str) -> str:
    return {
        "strong": "强信任",
        "normal": "正常信任",
        "observe": "观察区",
        "weak": "弱信任",
        "penalty": "惩罚区",
    }.get(status, "未知")


def _iso_from_ms(ms: int | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def _penalty_until_for(record: dict[str, Any], consecutive_losses: int, config: dict[str, Any]) -> str | None:
    if float(record.get("net_pnl") or 0) >= 0:
        return None
    close_time = int(record.get("close_time") or 0)
    hold_seconds = float(record.get("hold_seconds") or 0)
    quick_seconds = float(config.get("live_credit_quick_stop_seconds", 60))
    if consecutive_losses >= 3:
        hours = float(config.get("live_credit_three_loss_cooldown_hours", 24))
    elif consecutive_losses >= 2:
        hours = float(config.get("live_credit_two_loss_cooldown_hours", 4))
    elif hold_seconds and hold_seconds <= quick_seconds:
        hours = float(config.get("live_credit_quick_stop_cooldown_hours", 2))
    else:
        hours = float(config.get("live_credit_loss_cooldown_minutes", 30)) / 60
    return datetime.fromtimestamp(close_time / 1000, timezone.utc).replace(tzinfo=timezone.utc) + timedelta(hours=hours)


def score_records(records: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    records = sorted(records, key=lambda item: int(item.get("close_time") or 0))
    score = float(config.get("live_credit_default_score", DEFAULT_SCORE))
    wins = 0
    losses = 0
    consecutive_wins = 0
    consecutive_losses = 0
    gross_profit = 0.0
    gross_loss = 0.0
    net_pnl = 0.0
    commission = 0.0
    funding_fee = 0.0
    hold_sum = 0.0
    notes: list[str] = []
    penalty_until: datetime | None = None

    for record in records:
        net = float(record.get("net_pnl") or 0)
        fee = abs(float(record.get("commission") or 0))
        funding = float(record.get("funding_fee") or 0)
        hold_seconds = float(record.get("hold_seconds") or 0)
        notional = max(float(record.get("open_notional") or 0), 1.0)
        net_ratio = net / notional * 100
        net_pnl += net
        commission += fee
        funding_fee += funding
        hold_sum += hold_seconds

        if net > 0:
            wins += 1
            consecutive_wins += 1
            consecutive_losses = 0
            gross_profit += net
            delta = 3.0
            if net >= float(config.get("live_credit_big_win_usdt", 0.7)):
                delta += 2.0
            if net >= float(config.get("live_credit_large_win_usdt", 2.0)):
                delta += 4.0
            if consecutive_wins >= 2:
                delta += 3.0
            if consecutive_wins >= 3:
                delta += 5.0
            if fee > 0 and net > 0 and fee / max(net, 0.0001) <= 0.10:
                delta += 1.0
            if hold_seconds >= float(config.get("live_credit_min_quality_hold_seconds", 90)):
                delta += 1.0
            score += delta
            notes.append(f"盈利奖励 +{delta:.1f}")
        else:
            losses += 1
            consecutive_losses += 1
            consecutive_wins = 0
            gross_loss += abs(net)
            delta = -5.0
            if net <= -float(config.get("live_credit_big_loss_usdt", 0.7)):
                delta -= 3.0
            if net <= -float(config.get("live_credit_large_loss_usdt", 2.0)):
                delta -= 8.0
            if hold_seconds and hold_seconds <= float(config.get("live_credit_quick_stop_seconds", 60)):
                delta -= 8.0
            if consecutive_losses >= 2:
                delta -= 8.0
            if consecutive_losses >= 3:
                delta -= 15.0
            if abs(net_ratio) < 0.3 and fee > abs(net) * 0.20:
                delta -= 2.0
            score += delta
            notes.append(f"亏损惩罚 {delta:.1f}")
            until = _penalty_until_for(record, consecutive_losses, config)
            if until and (penalty_until is None or until > penalty_until):
                penalty_until = until

        score = max(0.0, min(100.0, score))

    closed = len(records)
    profit_factor = gross_profit / gross_loss if gross_loss else (999.0 if gross_profit > 0 else 0.0)
    status = _status_for_score(score)
    if status == "penalty" and penalty_until is None and records:
        close_time = int(records[-1].get("close_time") or 0)
        penalty_until = datetime.fromtimestamp(close_time / 1000, timezone.utc) + timedelta(
            hours=float(config.get("live_credit_penalty_cooldown_hours", 12))
        )
    return {
        "score": round(score, 2),
        "status": status,
        "status_label": status_label(status),
        "closed_trades": closed,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / closed * 100, 2) if closed else 0.0,
        "net_pnl": round(net_pnl, 8),
        "commission": round(commission, 8),
        "funding_fee": round(funding_fee, 8),
        "profit_factor": round(profit_factor, 4),
        "consecutive_wins": consecutive_wins,
        "consecutive_losses": consecutive_losses,
        "avg_hold_seconds": round(hold_sum / closed, 2) if closed else 0.0,
        "penalty_until": penalty_until.isoformat() if penalty_until else None,
        "last_trade_time": int(records[-1].get("close_time") or 0) if records else None,
        "notes": notes[-6:],
    }


def upsert_trade_records(records: list[dict[str, Any]]) -> int:
    init_live_learning_schema()
    inserted = 0
    with connect() as conn:
        for record in records:
            conn.execute(
                """
                INSERT OR REPLACE INTO live_trade_records (
                    symbol, direction, open_time, close_time, open_price, close_price,
                    quantity, open_notional, close_notional, realized_pnl, commission,
                    funding_fee, net_pnl, hold_seconds, trade_count, source, payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["symbol"],
                    record["direction"],
                    int(record["open_time"]),
                    int(record["close_time"]),
                    record.get("open_price"),
                    record.get("close_price"),
                    record.get("quantity"),
                    record.get("open_notional"),
                    record.get("close_notional"),
                    record.get("realized_pnl"),
                    record.get("commission"),
                    record.get("funding_fee", 0.0),
                    record.get("net_pnl"),
                    record.get("hold_seconds"),
                    record.get("trade_count"),
                    record.get("source", "binance"),
                    json.dumps(record.get("payload") or {}, ensure_ascii=False),
                    now_iso(),
                ),
            )
            inserted += 1
        conn.commit()
    return inserted


def rebuild_symbol_scores(config: dict[str, Any], lookback_hours: float | None = None) -> list[dict[str, Any]]:
    init_live_learning_schema()
    cutoff_ms = None
    if lookback_hours:
        cutoff_ms = int((time.time() - lookback_hours * 3600) * 1000)
    with connect() as conn:
        if cutoff_ms:
            rows = conn.execute(
                "SELECT * FROM live_trade_records WHERE close_time >= ? ORDER BY close_time ASC",
                (cutoff_ms,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM live_trade_records ORDER BY close_time ASC").fetchall()
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            item = dict(row)
            grouped.setdefault((item["symbol"], item["direction"]), []).append(item)
        results = []
        for (symbol, direction), records in grouped.items():
            score = score_records(records, config)
            conn.execute(
                """
                INSERT OR REPLACE INTO symbol_live_scores (
                    symbol, direction, score, status, closed_trades, wins, losses,
                    win_rate, net_pnl, commission, funding_fee, profit_factor,
                    consecutive_wins, consecutive_losses, avg_hold_seconds,
                    penalty_until, last_trade_time, notes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    direction,
                    score["score"],
                    score["status"],
                    score["closed_trades"],
                    score["wins"],
                    score["losses"],
                    score["win_rate"],
                    score["net_pnl"],
                    score["commission"],
                    score["funding_fee"],
                    score["profit_factor"],
                    score["consecutive_wins"],
                    score["consecutive_losses"],
                    score["avg_hold_seconds"],
                    score["penalty_until"],
                    score["last_trade_time"],
                    json.dumps(score["notes"], ensure_ascii=False),
                    now_iso(),
                ),
            )
            results.append({"symbol": symbol, "direction": direction, **score})
        conn.commit()
    return sorted(results, key=lambda item: (item["score"], item["net_pnl"]), reverse=True)


def list_live_scores(limit: int = 100) -> list[dict[str, Any]]:
    init_live_learning_schema()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM symbol_live_scores ORDER BY score DESC, net_pnl DESC LIMIT ?",
            (limit,),
        ).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        try:
            item["notes"] = json.loads(item.get("notes") or "[]")
        except json.JSONDecodeError:
            item["notes"] = []
        item["status_label"] = status_label(item.get("status", ""))
        item["last_trade_time_iso"] = _iso_from_ms(item.get("last_trade_time"))
        results.append(item)
    return results


def live_score_for(symbol: str, direction: str, config: dict[str, Any]) -> dict[str, Any]:
    if not config.get("live_credit_enabled", True):
        return {"enabled": False, "score": DEFAULT_SCORE, "status": "normal", "status_label": "未启用"}
    init_live_learning_schema()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM symbol_live_scores WHERE symbol = ? AND direction = ?",
            (symbol.upper(), direction.upper()),
        ).fetchone()
    if not row:
        return {
            "enabled": True,
            "score": float(config.get("live_credit_default_score", DEFAULT_SCORE)),
            "status": "new",
            "status_label": "新币观察",
            "closed_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "net_pnl": 0.0,
            "profit_factor": 0.0,
            "consecutive_wins": 0,
            "consecutive_losses": 0,
            "penalty_until": None,
            "notes": ["暂无实盘记录"],
        }
    item = dict(row)
    try:
        item["notes"] = json.loads(item.get("notes") or "[]")
    except json.JSONDecodeError:
        item["notes"] = []
    item["enabled"] = True
    item["status_label"] = status_label(item.get("status", ""))
    return item


def penalty_active(score: dict[str, Any]) -> bool:
    until = score.get("penalty_until")
    if not until:
        return False
    try:
        return datetime.fromisoformat(until) > datetime.now(timezone.utc)
    except ValueError:
        return False


def live_credit_multiplier(score: dict[str, Any], config: dict[str, Any]) -> float:
    value = float(score.get("score", DEFAULT_SCORE))
    status = str(score.get("status") or "")
    if status == "new":
        return 1.0
    if status == "penalty" or value < float(config.get("live_credit_penalty_score", 30)):
        return 0.0 if config.get("live_credit_penalty_observe_only", True) else 0.25
    if value >= float(config.get("live_credit_strong_score", 80)):
        return float(config.get("live_credit_max_risk_multiplier", 1.15))
    if value >= float(config.get("live_credit_normal_score", 65)):
        return 1.0
    if value >= float(config.get("live_credit_observe_score", 45)):
        return float(config.get("live_credit_observe_risk_multiplier", 0.70))
    return float(config.get("live_credit_weak_risk_multiplier", 0.45))


def apply_live_credit_to_candidate(candidate: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    if not config.get("live_credit_enabled", True):
        return candidate
    symbol = str(candidate.get("symbol") or "").upper()
    direction = str(candidate.get("direction") or "").upper()
    if not symbol or direction not in {"LONG", "SHORT"}:
        return candidate
    credit = live_score_for(symbol, direction, config)
    score = float(credit.get("score", DEFAULT_SCORE))
    weight = float(config.get("live_credit_score_weight", 0.35))
    score_delta = (score - float(config.get("live_credit_default_score", DEFAULT_SCORE))) * weight
    candidate = dict(candidate)
    candidate["live_credit"] = credit
    candidate["score"] = round(float(candidate.get("score") or 0) + score_delta, 4)
    reasons = [f"实盘信用 {score:.1f} 分（{credit.get('status_label', '-') }）"]
    multiplier = live_credit_multiplier(credit, config)
    if int(credit.get("consecutive_wins") or 0) >= int(config.get("live_credit_tail_win_count", 3)):
        tail_mult = float(config.get("live_credit_tail_risk_multiplier", 0.75))
        multiplier *= tail_mult
        candidate["score"] = round(float(candidate["score"]) - float(config.get("live_credit_tail_score_penalty", 3.0)), 4)
        reasons.append(f"连续盈利后防追尾，仓位 {tail_mult:.2f}x")
    if penalty_active(credit):
        candidate["passed"] = False
        candidate["reason"] = "live_credit_cooldown"
        candidate["decision_reason"] = f"实盘信用冷却中，暂停该币种方向到 {credit.get('penalty_until')}"
        multiplier = 0.0
    elif multiplier <= 0:
        candidate["passed"] = False
        candidate["reason"] = "live_credit_penalty"
        candidate["decision_reason"] = "实盘信用进入惩罚区，暂时只观察不实盘"
    else:
        candidate["risk_pct"] = float(candidate.get("risk_pct") or 0) * multiplier
        reasons.append(f"仓位倍率 {multiplier:.2f}x")
        if not candidate.get("decision_reason"):
            candidate["decision_reason"] = "；".join(reasons)
        else:
            candidate["decision_reason"] = f"{candidate['decision_reason']}；{'；'.join(reasons)}"
    candidate["live_credit_adjustment"] = {
        "score_delta": round(score_delta, 4),
        "risk_multiplier": round(multiplier, 4),
        "reasons": reasons,
    }
    return candidate


def _signed_open(position_side: str, side: str, quantity: float) -> float:
    if position_side == "LONG":
        return quantity if side == "BUY" else -quantity
    if position_side == "SHORT":
        return quantity if side == "SELL" else -quantity
    return quantity if side == "BUY" else -quantity


def build_trade_records_from_user_trades(
    trades_by_symbol: dict[str, list[dict[str, Any]]],
    income_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    funding_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in income_rows or []:
        if str(row.get("incomeType")) == "FUNDING_FEE" and row.get("symbol"):
            funding_by_symbol.setdefault(str(row["symbol"]).upper(), []).append(row)
    records: list[dict[str, Any]] = []
    for symbol, raw_trades in trades_by_symbol.items():
        active: dict[str, dict[str, Any]] = {}
        for trade in sorted(raw_trades, key=lambda item: int(item.get("time", 0))):
            direction = str(trade.get("positionSide") or "").upper()
            if direction not in {"LONG", "SHORT"}:
                direction = "LONG" if str(trade.get("side")).upper() == "BUY" else "SHORT"
            side = str(trade.get("side") or "").upper()
            quantity = float(trade.get("qty") or trade.get("quantity") or 0)
            price = float(trade.get("price") or 0)
            trade_time = int(trade.get("time") or 0)
            commission = abs(float(trade.get("commission") or 0))
            realized = float(trade.get("realizedPnl") or 0)
            signed = _signed_open(direction, side, quantity)
            item = active.get(direction)
            if item is None and signed > 0:
                item = {
                    "symbol": symbol.upper(),
                    "direction": direction,
                    "open_time": trade_time,
                    "open_notional": 0.0,
                    "close_notional": 0.0,
                    "quantity": 0.0,
                    "close_quantity": 0.0,
                    "realized_pnl": 0.0,
                    "commission": 0.0,
                    "trade_count": 0,
                    "payload": {"fills": []},
                }
                active[direction] = item
            if item is None:
                continue
            item["commission"] += commission
            item["realized_pnl"] += realized
            item["trade_count"] += 1
            item["payload"]["fills"].append(
                {
                    "time": trade_time,
                    "side": side,
                    "qty": quantity,
                    "price": price,
                    "realizedPnl": realized,
                    "commission": commission,
                }
            )
            if signed > 0:
                item["quantity"] += quantity
                item["open_notional"] += quantity * price
            else:
                item["close_quantity"] += quantity
                item["close_notional"] += quantity * price
                item["close_time"] = trade_time
            if item["quantity"] > 0 and item.get("close_quantity", 0.0) >= item["quantity"] - 1e-12:
                funding_fee = 0.0
                for funding in funding_by_symbol.get(symbol.upper(), []):
                    funding_time = int(funding.get("time") or 0)
                    if int(item["open_time"]) <= funding_time <= int(item["close_time"]):
                        funding_fee += float(funding.get("income") or 0)
                open_price = item["open_notional"] / item["quantity"] if item["quantity"] else 0.0
                close_price = item["close_notional"] / item["close_quantity"] if item["close_quantity"] else 0.0
                net_pnl = float(item["realized_pnl"]) - abs(float(item["commission"])) + funding_fee
                records.append(
                    {
                        **item,
                        "open_price": open_price,
                        "close_price": close_price,
                        "funding_fee": funding_fee,
                        "net_pnl": net_pnl,
                        "hold_seconds": (int(item["close_time"]) - int(item["open_time"])) / 1000,
                        "source": "binance",
                    }
                )
                active.pop(direction, None)
    return records


def sync_live_learning_from_binance(
    client: BinanceFuturesClient,
    config: dict[str, Any],
    lookback_hours: float | None = None,
) -> dict[str, Any]:
    if not config.get("live_credit_enabled", True):
        return {"enabled": False, "reason": "disabled"}
    lookback_hours = lookback_hours or float(config.get("live_credit_history_hours", 96))
    end_ms = int(time.time() * 1000)
    start_ms = int((time.time() - lookback_hours * 3600) * 1000)
    income = client.signed_request("GET", "/fapi/v1/income", {"startTime": start_ms, "endTime": end_ms, "limit": 1000})
    symbols = sorted({str(row.get("symbol", "")).upper() for row in income if row.get("symbol")})
    max_symbols = int(config.get("live_credit_sync_max_symbols", 20))
    symbols = symbols[:max_symbols]
    trades_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for symbol in symbols:
        rows = client.signed_request(
            "GET",
            "/fapi/v1/userTrades",
            {"symbol": symbol, "startTime": start_ms, "endTime": end_ms, "limit": 1000},
        )
        if rows:
            trades_by_symbol[symbol] = rows
    records = build_trade_records_from_user_trades(trades_by_symbol, income)
    upserted = upsert_trade_records(records)
    scores = rebuild_symbol_scores(config, lookback_hours=lookback_hours)
    result = {
        "enabled": True,
        "lookback_hours": lookback_hours,
        "symbols": symbols,
        "records": upserted,
        "scores": scores,
    }
    record_event("info", "live_learning", "实盘信用分已同步", result)
    return result
