from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.binance_client import BinanceFuturesClient
from app.exchange_filters import ExchangeFilters
from app.market_stream import stream_depth
from app.protection_audit import enrich_positions_with_prices
from app.risk import live_trading_allowed
from app.state_store import save_state
from app.strategy import atr
from app.telemetry import record_event, record_event_throttled


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _position_amount(position: dict[str, Any]) -> float:
    try:
        return float(position.get("positionAmt") or position.get("position_amt") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _position_symbol(position: dict[str, Any]) -> str:
    return str(position.get("symbol") or "").upper()


def _entry_price(position: dict[str, Any]) -> float:
    try:
        return float(position.get("entryPrice") or position.get("entry_price") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _mark_price(position: dict[str, Any]) -> float:
    try:
        return float(position.get("markPrice") or position.get("mark_price") or position.get("last_price") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _position_key(symbol: str, direction: str) -> str:
    return f"{symbol}:{direction}"


def _tracked_positions(state: dict[str, Any]) -> dict[str, Any]:
    tracked = state.get("runtime_protection_positions") or {}
    return tracked if isinstance(tracked, dict) else {}


def _orderbook_exit_signal(symbol: str, direction: str, config: dict[str, Any]) -> dict[str, Any]:
    depth = stream_depth(symbol, max_age_seconds=int(config.get("yolo_scalp_stream_max_age_seconds", 12)))
    if not depth:
        return {"enabled": True, "triggered": False, "reason": "depth_missing"}
    bids = depth.get("bids") or []
    asks = depth.get("asks") or []
    bid_notional = sum(float(price) * float(qty) for price, qty in bids[:5])
    ask_notional = sum(float(price) * float(qty) for price, qty in asks[:5])
    total = bid_notional + ask_notional
    imbalance = (bid_notional - ask_notional) / total if total > 0 else 0.0
    directed_imbalance = imbalance if direction == "LONG" else -imbalance
    spread_pct = float(depth.get("spread_pct") or 999.0)
    max_spread = float(config.get("yolo_scalp_orderbook_max_spread_pct", 0.08)) * float(
        config.get("yolo_scalp_orderbook_exit_spread_multiplier", 2.0)
    )
    reverse_threshold = -float(config.get("yolo_scalp_orderbook_exit_reverse_imbalance", 0.04))
    triggered = spread_pct > max_spread or directed_imbalance <= reverse_threshold
    return {
        "enabled": True,
        "triggered": triggered,
        "reason": "orderbook_invalid" if triggered else "orderbook_ok",
        "spread_pct": round(spread_pct, 6),
        "max_spread_pct": round(max_spread, 6),
        "directed_imbalance": round(directed_imbalance, 6),
        "reverse_threshold": round(reverse_threshold, 6),
    }


def _atr_value(client: BinanceFuturesClient, symbol: str, interval: str) -> float:
    bars = client.klines(symbol, interval, 120)
    values = atr(bars, 14)
    return float(values[-1]) if values else 0.0


def build_runtime_protection_action(
    position: dict[str, Any],
    *,
    config: dict[str, Any],
    state: dict[str, Any],
    client: BinanceFuturesClient | None = None,
) -> dict[str, Any]:
    symbol = _position_symbol(position)
    amount = _position_amount(position)
    if not symbol or amount == 0:
        return {"symbol": symbol, "action": "none", "reason": "no_position"}
    direction = "LONG" if amount > 0 else "SHORT"
    entry = _entry_price(position)
    mark = _mark_price(position)
    if entry <= 0 or mark <= 0:
        return {"symbol": symbol, "direction": direction, "action": "observe", "reason": "missing_price"}
    side = 1 if direction == "LONG" else -1
    pnl_pct = (mark - entry) / entry * side * 100
    adverse_pct = max(0.0, -pnl_pct)
    tracked = _tracked_positions(state)
    key = _position_key(symbol, direction)
    tracked_item = tracked.get(key) or {}
    opened_at_raw = (tracked.get(key) or {}).get("opened_at")
    opened_at = _now()
    if opened_at_raw:
        try:
            opened_at = datetime.fromisoformat(opened_at_raw)
            if opened_at.tzinfo is None:
                opened_at = opened_at.replace(tzinfo=timezone.utc)
        except ValueError:
            opened_at = _now()
    age_seconds = max(0.0, (_now() - opened_at).total_seconds())
    interval = str(config.get("extreme_sprint_interval") or config.get("interval") or "5m")
    atr_value = 0.0
    if client is not None:
        try:
            atr_value = _atr_value(client, symbol, interval)
        except Exception as exc:
            record_event("warning", "runtime_protection", f"ATR check failed for {symbol}: {exc}")
    atr_pct = atr_value / mark * 100 if mark > 0 and atr_value > 0 else 0.0
    fast_invalid_pct = atr_pct * float(config.get("protection_fast_invalid_atr", 0.35))
    fast_invalid_seconds = int(config.get("protection_fast_invalid_seconds", 90))
    max_hold_seconds = int((tracked.get(key) or {}).get("max_hold_seconds") or 0)
    max_hold_bars = int((tracked.get(key) or {}).get("max_hold_bars") or config.get("runtime_protection_max_hold_bars", 12))
    if max_hold_seconds <= 0:
        max_hold_seconds = max_hold_bars * 300
    break_even_trigger_pct = atr_pct * float(config.get("protection_break_even_trigger_atr", 0.55))
    trailing_trigger_pct = atr_pct * float(config.get("protection_trailing_trigger_atr", 0.9))
    trailing_distance_pct = atr_pct * float(config.get("protection_trailing_distance_atr", 0.55))
    action = "observe"
    reason = "holding"
    orderbook_exit = {"enabled": False}
    if (
        config.get("yolo_scalp_orderbook_runtime_exit_enabled", True)
        and tracked_item.get("entry_type") in {"orderbook_impact", "volume_scalp", "imbalance_probe"}
        and age_seconds >= 5
    ):
        orderbook_exit = _orderbook_exit_signal(symbol, direction, config)
        if orderbook_exit.get("triggered"):
            action = "close_orderbook_invalid"
            reason = "orderbook_invalid"
    if fast_invalid_seconds > 0 and age_seconds <= fast_invalid_seconds and fast_invalid_pct > 0 and adverse_pct >= fast_invalid_pct:
        action = "close_fast_invalid"
        reason = "fast_invalid"
    elif max_hold_seconds > 0 and age_seconds >= max_hold_seconds and pnl_pct < float(config.get("protection_min_profit_after_cost_pct", 0.08)):
        action = "close_time_stop"
        reason = "time_stop"
    elif trailing_trigger_pct > 0 and pnl_pct >= trailing_trigger_pct:
        action = "trail_stop"
        reason = "trailing_ready"
    elif break_even_trigger_pct > 0 and pnl_pct >= break_even_trigger_pct:
        action = "move_break_even"
        reason = "break_even_ready"
    return {
        "symbol": symbol,
        "direction": direction,
        "quantity": abs(amount),
        "entry": entry,
        "mark": mark,
        "pnl_pct": round(pnl_pct, 6),
        "age_seconds": round(age_seconds, 3),
        "atr_pct": round(atr_pct, 6),
        "fast_invalid_pct": round(fast_invalid_pct, 6),
        "trailing_distance_pct": round(trailing_distance_pct, 6),
        "orderbook_exit": orderbook_exit,
        "action": action,
        "reason": reason,
    }


def manage_runtime_protection(
    client: BinanceFuturesClient,
    config: dict[str, Any],
    state: dict[str, Any],
    account: dict[str, Any],
) -> dict[str, Any]:
    if not config.get("dynamic_protection_runtime_enabled", True):
        return {"enabled": False, "actions": []}
    positions = [item for item in account.get("positions", []) if abs(_position_amount(item)) > 0]
    if positions and client is not None:
        try:
            positions = enrich_positions_with_prices(positions, client.position_risk())
        except Exception as exc:
            record_event_throttled(
                "warning",
                "runtime_protection",
                f"positionRisk price fallback failed: {exc}",
                {},
                throttle_seconds=120,
            )
    tracked = _tracked_positions(state)
    updates: dict[str, Any] = {}
    now_iso = _now().isoformat()
    active_keys: set[str] = set()
    actions: list[dict[str, Any]] = []
    filters = None
    for position in positions:
        symbol = _position_symbol(position)
        direction = "LONG" if _position_amount(position) > 0 else "SHORT"
        key = _position_key(symbol, direction)
        active_keys.add(key)
        tracked.setdefault(key, {"opened_at": now_iso})
        action = build_runtime_protection_action(position, config=config, state={**state, "runtime_protection_positions": tracked}, client=client)
        actions.append(action)
        if action["action"] not in {"close_fast_invalid", "close_time_stop", "close_orderbook_invalid"}:
            continue
        if not (live_trading_allowed(config) and config.get("dynamic_protection_runtime_trade_enabled", False)):
            record_event("info", "runtime_protection", "runtime protection signal generated", action)
            continue
        if filters is None:
            filters = ExchangeFilters(client.exchange_info())
        close_side = "SELL" if direction == "LONG" else "BUY"
        quantity = filters.quantity(symbol, float(action["quantity"]))
        position_side = None
        try:
            if client.position_side_dual().get("dualSidePosition") is True:
                position_side = direction
        except Exception:
            position_side = None
        client.cancel_all_open_algo_orders(symbol)
        close_order = client.place_market_order(symbol, close_side, quantity, position_side=position_side)
        action["executed"] = True
        action["close_order"] = close_order
        record_event("warning", "runtime_protection", "runtime protection closed position", action)
    for key in list(tracked.keys()):
        if key not in active_keys:
            tracked.pop(key, None)
    updates["runtime_protection_positions"] = tracked
    save_state(updates)
    return {"enabled": True, "actions": actions}
