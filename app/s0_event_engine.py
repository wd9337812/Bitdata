from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STRATEGY_FAMILY = "s0_concentrated_event"
STRATEGY_VERSION = "v6.0-s0-event"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def event_state_path() -> Path:
    config_path = Path(os.getenv("APP_CONFIG_PATH", "./data/config.json"))
    return config_path.parent / "research" / "s0_forward_event_monitor" / "state.json"


def _read_monitor_state() -> dict[str, Any]:
    path = event_state_path()
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _direction(value: Any) -> str:
    return "LONG" if float(value or 0) > 0 else "SHORT"


def _age_seconds(event: dict[str, Any], now: datetime) -> float:
    ts = float(event.get("ts") or 0)
    if ts > 10_000_000_000:
        ts /= 1000.0
    return max(0.0, now.timestamp() - ts) if ts else float("inf")


def event_score(event: dict[str, Any]) -> float:
    """Score only a confirmed cross-market event. It is never a price forecast."""
    if event.get("type") != "polymarket_binance_confirmed":
        return 0.0
    probability_delta = abs(float(event.get("probability_delta") or 0.0))
    confirmations = int(event.get("binance_confirmation_count") or 0)
    volume_z = abs(float(event.get("volume_z") or 0.0))
    move_pct = abs(float(event.get("move_pct") or 0.0))
    return round(
        _clamp(45.0 + probability_delta * 250.0 + confirmations * 7.0 + volume_z * 2.0 + move_pct * 2.0, 0.0, 100.0),
        3,
    )


def event_engine_status(config: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    state = _read_monitor_state()
    events = list(state.get("latest_events") or [])
    max_age = max(30, int(config.get("s0_event_max_age_seconds", 300)))
    fresh = [item for item in events if _age_seconds(item, current) <= max_age]
    ranked = sorted(fresh, key=event_score, reverse=True)
    best = ranked[0] if ranked else None
    enabled = bool(config.get("s0_event_live_enabled", False))
    return {
        "enabled": enabled,
        "strategy_version": STRATEGY_VERSION,
        "strategy_family": STRATEGY_FAMILY,
        "monitor_updated_at": state.get("last_completed_at"),
        "monitor_age_seconds": max(0.0, time.time() - float(state.get("last_completed_epoch") or 0)) if state.get("last_completed_epoch") else None,
        "latest_event_count": len(events),
        "fresh_event_count": len(fresh),
        "best_event": best,
        "best_event_score": event_score(best or {}),
        "max_account_risk_pct": float(config.get("s0_event_s_grade_max_account_risk_pct", 50.0)),
        "max_leverage": int(config.get("s0_event_s_grade_max_leverage", 15)),
        "margin_pct": float(config.get("s0_event_margin_pct", 97.0)),
        "reason": "fresh_s_grade_event" if best else "no_fresh_cross_market_event",
    }


def build_s0_event_decision(
    config: dict[str, Any], state: dict[str, Any], account: dict[str, Any], now: datetime | None = None
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    status = event_engine_status(config, current)
    base = {
        "strategy_family": STRATEGY_FAMILY,
        "strategy_version": STRATEGY_VERSION,
        "mode": "s0_concentrated_event",
        "strategy": "s0_event_cross_market",
    }
    if not status["enabled"]:
        return {**base, "action": "WAIT", "reason": "s0_event_disabled", "risk": {"allowed": False, "reason": "s0_event_disabled"}, "event_status": status}
    event = status.get("best_event") or {}
    minimum_score = float(config.get("s0_event_s_grade_min_score", 82.0))
    score = event_score(event)
    if score < minimum_score:
        return {**base, "action": "WAIT", "reason": "s0_event_no_s_grade", "risk": {"allowed": False, "reason": "s0_event_no_s_grade"}, "event_status": status}
    if state.get("s0_daily_profit_lock_active"):
        return {**base, "action": "WAIT", "reason": "s0_daily_profit_lock", "risk": {"allowed": False, "reason": "s0_daily_profit_lock"}, "event_status": status}
    positions = [item for item in account.get("positions", []) if abs(float(item.get("positionAmt") or item.get("amount") or 0)) > 0]
    if positions:
        return {**base, "action": "WAIT", "reason": "s0_event_position_open", "risk": {"allowed": False, "reason": "max_open_positions"}, "event_status": status}
    symbol = str(event.get("symbol") or "").upper()
    entry = float(event.get("entry_price") or 0.0)
    direction = _direction(event.get("direction"))
    if not symbol or entry <= 0:
        return {**base, "action": "WAIT", "reason": "s0_event_invalid_market_data", "risk": {"allowed": False, "reason": "s0_event_invalid_market_data"}, "event_status": status}
    equity = max(0.0, float(account.get("equity") or 0.0))
    available = max(0.0, float(account.get("available_balance") or equity))
    hard_stop = max(0.0, float(config.get("hard_stop_equity", 5.0)))
    reserve = max(0.0, float(config.get("s0_event_hard_stop_reserve_usdt", 0.25)))
    if equity <= hard_stop + reserve:
        return {**base, "action": "WAIT", "reason": "s0_event_insufficient_hard_stop_headroom", "risk": {"allowed": False, "reason": "s0_event_insufficient_hard_stop_headroom"}, "event_status": status}
    stop_pct = _clamp(float(config.get("s0_event_stop_pct", 3.0)), 0.2, 20.0)
    target_r = _clamp(float(config.get("s0_event_take_profit_r", 2.0)), 0.5, 10.0)
    expected_cost_pct = max(0.01, float(config.get("taker_fee_pct_round_trip", 0.08)) + 2 * float(config.get("estimated_slippage_pct", 0.03)))
    loss_per_notional_pct = stop_pct + expected_cost_pct
    configured_risk = _clamp(float(config.get("s0_event_s_grade_max_account_risk_pct", 50.0)), 1.0, 50.0)
    hard_stop_cap = max(0.0, (equity - hard_stop - reserve) / equity * 100.0)
    risk_pct = min(configured_risk, hard_stop_cap)
    margin_pct = _clamp(float(config.get("s0_event_margin_pct", 97.0)), 1.0, 99.0)
    leverage = max(1, min(20, int(config.get("s0_event_s_grade_max_leverage", 15))))
    liquidation_buffer = 100.0 / leverage - stop_pct
    min_buffer = float(config.get("s0_event_min_liquidation_buffer_pct", 2.0))
    if liquidation_buffer < min_buffer:
        return {**base, "action": "WAIT", "reason": "s0_event_liquidation_buffer_too_small", "risk": {"allowed": False, "reason": "s0_event_liquidation_buffer_too_small"}, "event_status": status}
    risk_notional = equity * risk_pct / max(loss_per_notional_pct, 0.01)
    margin_notional = min(equity, available) * margin_pct / 100.0 * leverage
    notional = min(risk_notional, margin_notional)
    quantity = notional / entry
    sign = 1.0 if direction == "LONG" else -1.0
    stop = entry * (1.0 - sign * stop_pct / 100.0)
    take_profit = entry * (1.0 + sign * stop_pct * target_r / 100.0)
    event_id = str(event.get("event_id") or f"{symbol}:{event.get('ts')}")
    source_market_id = str(event.get("source_market_id") or event_id)
    if event_id == str(state.get("s0_event_last_traded_id") or ""):
        return {**base, "action": "WAIT", "reason": "s0_event_already_traded", "risk": {"allowed": False, "reason": "s0_event_already_traded"}, "event_status": status}
    recent_events = dict(state.get("s0_event_recent_market_ids") or {})
    recent_at = float(recent_events.get(source_market_id) or 0.0)
    if recent_at and current.timestamp() - recent_at < int(config.get("s0_event_max_hold_seconds", 21_600)):
        return {**base, "action": "WAIT", "reason": "s0_event_source_cooldown", "risk": {"allowed": False, "reason": "s0_event_source_cooldown"}, "event_status": status}
    candidate = {
        "symbol": symbol,
        "direction": direction,
        "strategy_family": STRATEGY_FAMILY,
        "strategy_version": STRATEGY_VERSION,
        "strategy_role": "active",
        "passed": True,
        "event": {**event, "score": score, "event_id": event_id},
        "estimated_cost_pct": expected_cost_pct,
        "signal": {
            "signal": direction,
            "last_price": entry,
            "stop": stop,
            "take_profit": take_profit,
            "protection_profile": {
                "protection_version": "s0_event_v6_sgrade",
                "max_hold_seconds": int(config.get("s0_event_max_hold_seconds", 21_600)),
                "runtime_intraday_trailing_enabled": True,
                "break_even_trigger_pct": stop_pct,
                "trailing_trigger_pct": stop_pct * target_r * 0.6,
                "trailing_distance_pct": stop_pct,
            },
        },
    }
    return {
        **base,
        "symbol": symbol,
        "action": f"OPEN_{direction}",
        "direction": direction,
        "signal": candidate["signal"],
        "candidate": candidate,
        "risk": {"allowed": True, "reason": "s0_event_s_grade", "max_notional": notional},
        "quantity": quantity,
        "estimated_notional": notional,
        "risk_pct": risk_pct,
        "leverage": leverage,
        "entry_type": "s0_cross_market_s_grade",
        "decision_reason": "S级事件：预测市场概率突变与币安价格、放量确认一致",
        "equity": equity,
        "full_bet_sizing": {"sizing_available_balance": available, "margin_utilization_pct": margin_pct, "stressed_risk_pct": risk_pct, "profile": "s0_event_v6_s_grade"},
        "event_status": status,
        "event_id": event_id,
        "source_market_id": source_market_id,
    }
