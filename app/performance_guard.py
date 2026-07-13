from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.telemetry import connect, db_path


_CACHE: dict[str, tuple[float, Any]] = {}


def _profit_factor(positive: float, negative: float) -> float:
    if negative < 0:
        return positive / abs(negative)
    return 999.0 if positive > 0 else 0.0


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    positive = sum(float(row.get("net_pnl") or 0) for row in rows if float(row.get("net_pnl") or 0) > 0)
    negative = sum(float(row.get("net_pnl") or 0) for row in rows if float(row.get("net_pnl") or 0) < 0)
    wins = sum(1 for row in rows if float(row.get("net_pnl") or 0) > 0)
    costs = sum(
        abs(float(row.get("commission") or row.get("estimated_cost") or 0))
        + abs(float(row.get("funding_fee") or 0))
        for row in rows
    )
    return {
        "trades": len(rows),
        "wins": wins,
        "win_rate": round(wins / len(rows) * 100, 2) if rows else 0.0,
        "net_pnl": round(positive + negative, 8),
        "cost": round(costs, 8),
        "profit_factor": round(_profit_factor(positive, negative), 4),
    }


def _cached(key: str, ttl_seconds: float, loader: Any) -> Any:
    cache_key = f"{db_path()}:{key}"
    now = time.monotonic()
    cached = _CACHE.get(cache_key)
    if cached and now - cached[0] <= ttl_seconds:
        return cached[1]
    value = loader()
    _CACHE[cache_key] = (now, value)
    return value


def clear_performance_cache() -> None:
    _CACHE.clear()


def global_performance_guard(
    config: dict[str, Any],
    equity: float | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not config.get("performance_guard_enabled", True):
        return {"enabled": False, "allowed": True, "status": "disabled", "risk_multiplier": 1.0}
    now = now or datetime.now(timezone.utc)
    live_limit = int(config.get("performance_guard_live_window_trades", 10))
    shadow_limit = int(config.get("performance_guard_shadow_window_trades", 100))

    def load() -> dict[str, Any]:
        with connect() as conn:
            try:
                live = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT close_time, net_pnl, commission, funding_fee FROM live_trade_records ORDER BY close_time DESC LIMIT ?",
                        (live_limit,),
                    ).fetchall()
                ]
            except sqlite3.OperationalError:
                live = []
            try:
                shadow = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT closed_at, net_pnl, estimated_cost FROM shadow_trades WHERE status = 'CLOSED' ORDER BY id DESC LIMIT ?",
                        (shadow_limit,),
                    ).fetchall()
                ]
            except sqlite3.OperationalError:
                shadow = []
            peak_equity = None
            try:
                peak_cutoff = (
                    now - timedelta(hours=float(config.get("performance_guard_peak_lookback_hours", 24)))
                ).isoformat()
                row = conn.execute(
                    "SELECT MAX(equity) AS peak_equity FROM equity_snapshots WHERE ts >= ? AND equity > 0",
                    (peak_cutoff,),
                ).fetchone()
                peak_equity = float(row["peak_equity"] or 0) if row else None
            except sqlite3.OperationalError:
                peak_equity = None
        return {"live_rows": live, "shadow_rows": shadow, "peak_equity": peak_equity}

    raw = _cached("global", float(config.get("performance_guard_cache_seconds", 15)), load)
    live_rows = raw["live_rows"]
    shadow_rows = raw["shadow_rows"]
    live = _stats(live_rows)
    shadow = _stats(shadow_rows)
    min_live = int(config.get("performance_guard_min_live_trades", 10))
    min_shadow = int(config.get("performance_guard_min_shadow_trades", 50))
    max_pf = float(config.get("performance_guard_max_bad_profit_factor", 0.8))
    max_win_rate = float(config.get("performance_guard_max_bad_win_rate", 25.0))
    tail_losses = 0
    for row in live_rows:
        if float(row.get("net_pnl") or 0) > 0:
            break
        tail_losses += 1
    rolling_losses = sum(1 for row in live_rows if float(row.get("net_pnl") or 0) <= 0)
    live_bad = (
        live["trades"] >= min_live
        and live["net_pnl"] < 0
        and live["profit_factor"] < max_pf
        and live["win_rate"] < max_win_rate
    )
    shadow_bad = (
        shadow["trades"] >= min_shadow
        and shadow["net_pnl"] < 0
        and shadow["profit_factor"] < max_pf
    )
    window_loss_pct = (
        abs(min(0.0, float(live.get("net_pnl") or 0))) / max(float(equity or 0), 0.00000001) * 100
        if equity
        else 0.0
    )
    live_severe = bool(
        live["trades"] >= min_live
        and live["net_pnl"] < 0
        and (
            live["profit_factor"] < float(config.get("performance_guard_severe_profit_factor", 0.5))
            or live["win_rate"] < float(config.get("performance_guard_severe_win_rate", 20.0))
            or tail_losses >= int(config.get("performance_guard_severe_consecutive_losses", 4))
            or window_loss_pct >= float(config.get("performance_guard_severe_window_loss_equity_pct", 8.0))
        )
    )
    peak_equity = float(raw.get("peak_equity") or 0)
    peak_drawdown_pct = (
        max(0.0, (peak_equity - float(equity)) / peak_equity * 100)
        if peak_equity > 0 and equity is not None
        else 0.0
    )
    peak_drawdown_severe = peak_drawdown_pct >= float(config.get("performance_guard_peak_drawdown_pct", 12.0))
    risk_off = live_severe or peak_drawdown_severe or (live_bad and shadow_bad)
    latest_close_ms = int(live_rows[0].get("close_time") or 0) if live_rows else 0
    latest_close = datetime.fromtimestamp(latest_close_ms / 1000, timezone.utc) if latest_close_ms else None
    pause_minutes = float(config.get("performance_guard_pause_minutes", 60))
    pause_until = (latest_close or now) + timedelta(minutes=pause_minutes) if risk_off else None
    cooldown_active = bool(pause_until and now < pause_until)
    live_tail = _stats(live_rows[: max(3, min_live // 2)])
    shadow_tail = _stats(shadow_rows[: max(10, min_shadow // 3)])
    recovery_level = 0
    recovery_multiplier = float(config.get("performance_guard_recovery_risk_multiplier", 0.2))
    if risk_off and not cooldown_active:
        one_side_recovering = (
            (live_tail["trades"] >= 3 and live_tail["net_pnl"] > 0 and live_tail["profit_factor"] >= 0.9)
            or (shadow_tail["trades"] >= 10 and shadow_tail["net_pnl"] > 0 and shadow_tail["profit_factor"] >= 0.9)
        )
        both_recovering = (
            live_tail["trades"] >= 3
            and shadow_tail["trades"] >= 10
            and live_tail["net_pnl"] > 0
            and shadow_tail["net_pnl"] > 0
            and live_tail["profit_factor"] >= 1.0
            and shadow_tail["profit_factor"] >= 1.0
        )
        if both_recovering:
            recovery_level = 3
            recovery_multiplier = float(config.get("performance_guard_recovery_level_3_multiplier", 0.70))
        elif one_side_recovering:
            recovery_level = 2
            recovery_multiplier = float(config.get("performance_guard_recovery_level_2_multiplier", 0.40))
        else:
            recovery_level = 1
            recovery_multiplier = float(config.get("performance_guard_recovery_level_1_multiplier", recovery_multiplier))
    status = (
        "cooldown"
        if cooldown_active
        else f"recovery_{recovery_level}"
        if risk_off and recovery_level >= 2
        else "risk_off"
        if risk_off
        else "normal"
    )
    labels = {
        "normal": "正常",
        "cooldown": "暂停新开仓",
        "risk_off": "等待影子验证恢复",
        "recovery_2": "二级小仓恢复",
        "recovery_3": "三级受限恢复",
    }
    allowed = not cooldown_active and (not risk_off or recovery_level >= 2)
    reason = "滚动表现正常"
    if live_severe:
        reason = "实盘短窗口发生严重恶化，影子交易不得否决紧急暂停"
    elif peak_drawdown_severe:
        reason = f"权益高点回撤 {peak_drawdown_pct:.2f}% 已触发保护"
    elif live_bad and shadow_bad:
        reason = "实盘与影子交易同时处于负期望"
    elif risk_off:
        reason = "等待影子交易证明恢复后才允许小仓验证"
    return {
        "enabled": True,
        "allowed": allowed,
        "status": status,
        "status_label": labels[status],
        "risk_multiplier": 0.0 if not allowed else recovery_multiplier if risk_off else 1.0,
        "pause_until": pause_until.isoformat() if pause_until else None,
        "live": live,
        "shadow": shadow,
        "live_tail": live_tail,
        "shadow_tail": shadow_tail,
        "recovery_level": recovery_level,
        "live_bad": live_bad,
        "shadow_bad": shadow_bad,
        "live_severe": live_severe,
        "window_loss_equity_pct": round(window_loss_pct, 4),
        "peak_equity": round(peak_equity, 8) if peak_equity else None,
        "peak_drawdown_pct": round(peak_drawdown_pct, 4),
        "peak_drawdown_severe": peak_drawdown_severe,
        "rolling_losses": rolling_losses,
        "tail_losses": tail_losses,
        "equity": equity,
        "reason": reason,
    }


def _evidence_rows(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    hours = float(config.get("strategy_evidence_window_hours", 24))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff_ms = int(cutoff.timestamp() * 1000)
    cutoff_iso = cutoff.isoformat()
    result: dict[str, dict[str, Any]] = {}
    with connect() as conn:
        try:
            live_rows = conn.execute(
                "SELECT symbol, direction, close_time, net_pnl, commission, funding_fee FROM live_trade_records WHERE close_time >= ?",
                (cutoff_ms,),
            ).fetchall()
        except sqlite3.OperationalError:
            live_rows = []
        try:
            shadow_rows = conn.execute(
                "SELECT symbol, direction, closed_at, net_pnl, estimated_cost FROM shadow_trades WHERE status = 'CLOSED' AND closed_at >= ?",
                (cutoff_iso,),
            ).fetchall()
        except sqlite3.OperationalError:
            shadow_rows = []
    for source, rows in (("live", live_rows), ("shadow", shadow_rows)):
        grouped: dict[str, list[dict[str, Any]]] = {}
        direction_grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            item = dict(row)
            direction = str(item.get("direction") or "").upper()
            key = f"{str(item.get('symbol') or '').upper()}:{direction}"
            grouped.setdefault(key, []).append(item)
            direction_grouped.setdefault(f"*:{direction}", []).append(item)
        for key, items in grouped.items():
            result.setdefault(key, {})[source] = _stats(items)
            if source == "live":
                latest = max(items, key=lambda item: int(item.get("close_time") or 0))
                result[key]["latest_live_close_time"] = int(latest.get("close_time") or 0)
                result[key]["latest_live_net_pnl"] = float(latest.get("net_pnl") or 0)
        for key, items in direction_grouped.items():
            result.setdefault(key, {})[source] = _stats(items)
    return result


def strategy_evidence_for(symbol: str, direction: str, config: dict[str, Any]) -> dict[str, Any]:
    if not config.get("strategy_evidence_enabled", True):
        return {"enabled": False, "agreement": "disabled", "risk_cap": 1.0, "boost_allowed": True}
    evidence_map = _cached(
        "evidence",
        float(config.get("performance_guard_cache_seconds", 15)),
        lambda: _evidence_rows(config),
    )
    item = evidence_map.get(f"{symbol.upper()}:{direction.upper()}", {})
    direction_item = evidence_map.get(f"*:{direction.upper()}", {})
    live = item.get("live") or _stats([])
    shadow = item.get("shadow") or _stats([])
    live_positive = (
        live["trades"] >= int(config.get("strategy_evidence_live_confirm_trades", 8))
        and live["net_pnl"] > 0
        and live["profit_factor"] >= float(config.get("strategy_evidence_live_confirm_pf", 1.1))
    )
    shadow_positive = (
        shadow["trades"] >= int(config.get("strategy_evidence_shadow_confirm_trades", 30))
        and shadow["net_pnl"] > 0
        and shadow["profit_factor"] >= float(config.get("strategy_evidence_shadow_confirm_pf", 1.2))
    )
    live_negative = (
        live["trades"] >= int(config.get("strategy_evidence_live_negative_trades", 2))
        and live["net_pnl"] < 0
        and live["profit_factor"] < float(config.get("strategy_evidence_negative_pf", 0.8))
    )
    shadow_negative = (
        shadow["trades"] >= int(config.get("strategy_evidence_shadow_negative_trades", 10))
        and shadow["net_pnl"] < 0
        and shadow["profit_factor"] < float(config.get("strategy_evidence_negative_pf", 0.8))
    )
    if live_positive and shadow_positive:
        agreement = "positive"
        risk_cap = float(config.get("strategy_evidence_positive_risk_cap", 1.15))
    elif live_negative and shadow_negative:
        agreement = "negative"
        risk_cap = float(config.get("strategy_evidence_negative_risk_cap", 0.25))
    elif live_negative or shadow_negative:
        agreement = "weak"
        risk_cap = float(config.get("strategy_evidence_weak_risk_cap", 0.5))
    else:
        agreement = "insufficient"
        risk_cap = 1.0
    latest_close_ms = int(item.get("latest_live_close_time") or 0)
    latest_loss = float(item.get("latest_live_net_pnl") or 0) < 0
    reentry_until = None
    if latest_close_ms and latest_loss:
        reentry_until = datetime.fromtimestamp(latest_close_ms / 1000, timezone.utc) + timedelta(
            minutes=float(config.get("strategy_evidence_loss_reentry_minutes", 30))
        )
    reentry_blocked = bool(reentry_until and datetime.now(timezone.utc) < reentry_until)
    direction_live = direction_item.get("live") or _stats([])
    direction_shadow = direction_item.get("shadow") or _stats([])
    direction_negative = (
        direction_live["trades"] >= max(5, int(config.get("strategy_evidence_live_negative_trades", 2)))
        and direction_shadow["trades"] >= max(30, int(config.get("strategy_evidence_shadow_negative_trades", 10)))
        and direction_live["net_pnl"] < 0
        and direction_shadow["net_pnl"] < 0
        and direction_live["profit_factor"] < float(config.get("strategy_evidence_negative_pf", 0.8))
        and direction_shadow["profit_factor"] < float(config.get("strategy_evidence_negative_pf", 0.8))
    )
    if direction_negative:
        risk_cap = min(risk_cap, float(config.get("strategy_evidence_direction_negative_risk_cap", 0.5)))
    return {
        "enabled": True,
        "agreement": agreement,
        "agreement_label": {
            "positive": "影子与实盘共同盈利",
            "negative": "影子与实盘共同亏损",
            "weak": "一侧出现明确负向证据",
            "insufficient": "样本不足，禁止信用加仓",
        }[agreement],
        "risk_cap": round(risk_cap, 4),
        "boost_allowed": live_positive and shadow_positive,
        "reentry_blocked": reentry_blocked,
        "reentry_until": reentry_until.isoformat() if reentry_until else None,
        "live": live,
        "shadow": shadow,
        "direction": {
            "negative": direction_negative,
            "live": direction_live,
            "shadow": direction_shadow,
        },
    }


def apply_strategy_evidence_to_candidate(candidate: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    symbol = str(candidate.get("symbol") or "").upper()
    direction = str(candidate.get("direction") or "").upper()
    if not symbol or direction not in {"LONG", "SHORT"}:
        return candidate
    if str(candidate.get("strategy_family") or "") == "extreme_v3_roll":
        candidate = dict(candidate)
        candidate["strategy_evidence"] = {
            "enabled": True,
            "agreement": "isolated_warmup",
            "agreement_label": "V3 独立样本积累中",
            "risk_cap": 1.0,
            "boost_allowed": False,
        }
        message = "证据校验：V3 与旧策略样本隔离，当前只积累本策略实盘与影子结果"
        candidate["decision_reason"] = f"{candidate.get('decision_reason')}；{message}" if candidate.get("decision_reason") else message
        return candidate
    evidence = strategy_evidence_for(symbol, direction, config)
    if not evidence.get("enabled"):
        return candidate
    candidate = dict(candidate)
    adjustment = dict(candidate.get("live_credit_adjustment") or {})
    input_risk = float(adjustment.get("input_risk_pct") or candidate.get("risk_pct") or 0)
    cap = float(evidence.get("risk_cap") or 1.0)
    candidate["risk_pct"] = round(min(float(candidate.get("risk_pct") or 0), input_risk * cap), 8)
    if not evidence.get("boost_allowed") and adjustment:
        adjustment["risk_multiplier"] = min(float(adjustment.get("risk_multiplier") or 1.0), cap)
        adjustment["boost_qualified"] = False
        failures = list(adjustment.get("boost_failures") or [])
        if "shadow_live_not_confirmed" not in failures:
            failures.append("shadow_live_not_confirmed")
        adjustment["boost_failures"] = failures
        candidate["live_credit_adjustment"] = adjustment
    if evidence.get("reentry_blocked"):
        candidate["passed"] = False
        candidate["reason"] = "strategy_evidence_reentry_cooldown"
    message = f"证据校验：{evidence.get('agreement_label')}，风险上限 {cap:.2f}x"
    if evidence.get("reentry_blocked"):
        message += f"，亏损后同向等待至 {evidence.get('reentry_until')}"
    candidate["decision_reason"] = f"{candidate.get('decision_reason')}；{message}" if candidate.get("decision_reason") else message
    candidate["strategy_evidence"] = evidence
    return candidate


def observed_round_trip_cost_pct(config: dict[str, Any]) -> float:
    limit = int(config.get("observed_cost_window_trades", 50))

    def load() -> float:
        with connect() as conn:
            try:
                rows = conn.execute(
                    "SELECT open_notional, commission, funding_fee FROM live_trade_records WHERE open_notional > 0 ORDER BY close_time DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        values = sorted(
            (
                (abs(float(row["commission"] or 0)) + abs(float(row["funding_fee"] or 0)))
                / float(row["open_notional"])
                * 100
            )
            for row in rows
            if float(row["open_notional"] or 0) > 0
        )
        if not values:
            return float(config.get("taker_fee_pct_round_trip", 0.08)) + float(config.get("estimated_slippage_pct", 0.04))
        index = min(len(values) - 1, max(0, int(len(values) * 0.75)))
        return values[index] * float(config.get("observed_cost_safety_multiplier", 1.15))

    return float(_cached("observed_cost", float(config.get("performance_guard_cache_seconds", 15)), load))
