from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.binance_client import BinanceFuturesClient
from app.binance_rate import request_priority
from app.exchange_filters import ExchangeFilters
from app.market_stream import stream_depth, stream_ticker
from app.protection_audit import audit_position_protection, enrich_positions_with_prices
from app.risk import live_trading_allowed
from app.state_store import load_state, save_state
from app.strategy import atr
from app.telemetry import record_event, record_event_throttled
from app.strategy_capabilities import strategy_supports


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


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


def _merge_active_tracking_with_latest(
    tracked: dict[str, Any],
    active_keys: set[str],
) -> dict[str, Any]:
    """Preserve richer position metadata written concurrently by the entry path."""
    latest = _tracked_positions(load_state())
    return {
        key: {
            **dict(latest.get(key) or {}),
            **dict(tracked.get(key) or {}),
        }
        for key in active_keys
    }


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


def _algo_type(order: dict[str, Any]) -> str:
    return str(order.get("orderType") or order.get("type") or "").upper()


def _algo_trigger(order: dict[str, Any]) -> float:
    try:
        return float(order.get("triggerPrice") or order.get("stopPrice") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _algo_id(order: dict[str, Any]) -> int | str | None:
    return order.get("algoId") or order.get("orderId")


def _algo_closes_position(order: dict[str, Any]) -> bool:
    value = order.get("closePosition")
    if isinstance(value, bool):
        return value
    return str(value or "").lower() == "true"


def _replace_dynamic_stop(
    client: BinanceFuturesClient,
    filters: ExchangeFilters,
    position: dict[str, Any],
    action: dict[str, Any],
    tracked_item: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Tighten protection by placing and confirming the new stop before removing the old one."""
    symbol = _position_symbol(position)
    direction = str(action.get("direction") or "LONG")
    entry = float(action.get("entry") or 0)
    mark = float(action.get("mark") or 0)
    atr_value = mark * float(action.get("atr_pct") or 0) / 100
    if entry <= 0 or mark <= 0 or atr_value <= 0:
        return {**action, "executed": False, "management_status": "price_or_atr_missing"}
    last_raw = tracked_item.get("last_stop_adjustment_at")
    if last_raw:
        try:
            last = datetime.fromisoformat(str(last_raw).replace("Z", "+00:00"))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            minimum = int(config.get("runtime_stop_management_min_interval_seconds", 30))
            if (_now() - last).total_seconds() < minimum:
                return {**action, "executed": False, "management_status": "adjustment_interval"}
        except ValueError:
            pass
    is_short = direction == "SHORT"
    if action.get("action") == "trail_stop":
        distance_atr = float(tracked_item.get("trailing_distance_atr") or config.get("protection_trailing_distance_atr", 0.55))
        desired = mark + atr_value * distance_atr if is_short else mark - atr_value * distance_atr
    else:
        buffer_pct = float(config.get("protection_break_even_buffer_pct", 0.08)) / 100
        desired = entry * (1 - buffer_pct) if is_short else entry * (1 + buffer_pct)
    desired = filters.price(symbol, desired)
    orders = client.open_algo_orders(symbol)
    stops = [order for order in orders if _algo_type(order) == "STOP_MARKET"]
    triggers = [_algo_trigger(order) for order in stops if _algo_trigger(order) > 0]
    current = min(triggers) if is_short and triggers else max(triggers) if triggers else 0.0
    minimum_improvement = atr_value * float(config.get("runtime_stop_management_min_improvement_atr", 0.10))
    improves = desired < current - minimum_improvement if is_short and current > 0 else desired > current + minimum_improvement
    valid_side = desired > mark if is_short else desired < mark
    if not improves or not valid_side:
        return {
            **action,
            "executed": False,
            "management_status": "no_safe_improvement",
            "current_stop": current,
            "desired_stop": desired,
        }
    close_side = "BUY" if is_short else "SELL"
    position_side = str(position.get("positionSide") or "").upper()
    if position_side not in {"LONG", "SHORT"}:
        position_side = None
    if any(_algo_closes_position(order) for order in stops):
        # Binance conditional orders cannot be amended. In one-way mode a
        # confirmed reduce-only quantity stop can bridge the replace without a
        # protection gap. Hedge mode cannot use reduceOnly, so it deliberately
        # keeps the old closePosition stop and skips the add-on path.
        if position_side is not None:
            return {
                **action,
                "executed": False,
                "management_status": "exchange_atomic_replace_unavailable_hedge_mode",
                "current_stop": current,
                "desired_stop": desired,
            }
        quantity = filters.quantity(symbol, abs(_position_amount(position)))
        if quantity <= 0:
            return {
                **action,
                "executed": False,
                "management_status": "bridge_quantity_invalid",
                "current_stop": current,
                "desired_stop": desired,
            }
        bridge_order = client.place_algo_order(
            symbol=symbol,
            side=close_side,
            order_type="STOP_MARKET",
            trigger_price=desired,
            close_position=False,
            quantity=quantity,
            reduce_only=True,
        )
        bridge_matches = [
            order
            for order in client.open_algo_orders(symbol)
            if _algo_type(order) == "STOP_MARKET"
            and not _algo_closes_position(order)
            and abs(_algo_trigger(order) - desired) <= max(abs(desired) * 0.000001, 0.00000001)
        ]
        if not bridge_matches:
            return {
                **action,
                "executed": False,
                "management_status": "bridge_stop_not_confirmed",
                "current_stop": current,
                "desired_stop": desired,
                "bridge_order": bridge_order,
            }
        bridge_id = _algo_id(bridge_matches[-1])
        cancelled: list[int | str] = []
        cancel_errors: list[str] = []
        for order in stops:
            old_id = _algo_id(order)
            if old_id is None or old_id == bridge_id:
                continue
            try:
                client.cancel_algo_order(old_id)
                cancelled.append(old_id)
            except Exception as exc:
                cancel_errors.append(str(exc))
        if cancel_errors:
            if bridge_id is not None:
                try:
                    client.cancel_algo_order(bridge_id)
                except Exception:
                    pass
            return {
                **action,
                "executed": False,
                "management_status": "old_stop_cancel_failed_bridge_removed",
                "current_stop": current,
                "desired_stop": desired,
                "bridge_order": bridge_order,
                "cancelled_algo_ids": cancelled,
                "cancel_errors": cancel_errors,
            }
        final_order = client.place_algo_order(
            symbol=symbol,
            side=close_side,
            order_type="STOP_MARKET",
            trigger_price=desired,
            close_position=True,
        )
        final_matches = [
            order
            for order in client.open_algo_orders(symbol)
            if _algo_type(order) == "STOP_MARKET"
            and _algo_closes_position(order)
            and abs(_algo_trigger(order) - desired) <= max(abs(desired) * 0.000001, 0.00000001)
        ]
        if not final_matches:
            # The confirmed reduce-only bridge remains active. Do not add to the
            # position until a full closePosition stop can be confirmed.
            return {
                **action,
                "executed": False,
                "management_status": "bridge_stop_retained_final_unconfirmed",
                "current_stop": current,
                "desired_stop": desired,
                "bridge_order": bridge_order,
                "final_order": final_order,
                "cancelled_algo_ids": cancelled,
            }
        final_id = _algo_id(final_matches[-1])
        bridge_cancelled = False
        if bridge_id is not None:
            try:
                client.cancel_algo_order(bridge_id)
                bridge_cancelled = True
            except Exception as exc:
                # Both orders are reduce-only/close-position in one-way mode, so
                # they cannot reverse the account. Still skip adding until the
                # duplicate bridge can be cleaned up.
                return {
                    **action,
                    "executed": False,
                    "management_status": "final_confirmed_bridge_cleanup_pending",
                    "current_stop": current,
                    "desired_stop": desired,
                    "bridge_order": bridge_order,
                    "final_order": final_order,
                    "final_algo_id": final_id,
                    "bridge_cancel_error": str(exc),
                }
        tracked_item.update(
            {
                "managed_stop": desired,
                "last_stop_adjustment_at": _now().isoformat(),
                "last_stop_action": action.get("action"),
            }
        )
        return {
            **action,
            "executed": True,
            "management_status": "stop_tightened_with_reduce_only_bridge",
            "previous_stop": current,
            "desired_stop": desired,
            "bridge_order": bridge_order,
            "new_order": final_order,
            "bridge_cancelled": bridge_cancelled,
            "cancelled_algo_ids": cancelled,
        }
    new_order = client.place_algo_order(
        symbol=symbol,
        side=close_side,
        order_type="STOP_MARKET",
        trigger_price=desired,
        position_side=position_side,
    )
    confirmed = [
        order
        for order in client.open_algo_orders(symbol)
        if _algo_type(order) == "STOP_MARKET" and abs(_algo_trigger(order) - desired) <= max(abs(desired) * 0.000001, 0.00000001)
    ]
    if not confirmed:
        return {**action, "executed": False, "management_status": "new_stop_not_confirmed", "new_order": new_order}
    new_id = _algo_id(confirmed[-1])
    cancelled: list[int | str] = []
    cancel_errors: list[str] = []
    for order in stops:
        old_id = _algo_id(order)
        if old_id is None or old_id == new_id:
            continue
        try:
            client.cancel_algo_order(old_id)
            cancelled.append(old_id)
        except Exception as exc:
            cancel_errors.append(str(exc))
    tracked_item.update(
        {
            "managed_stop": desired,
            "last_stop_adjustment_at": _now().isoformat(),
            "last_stop_action": action.get("action"),
        }
    )
    return {
        **action,
        "executed": True,
        "management_status": "stop_tightened" if not cancel_errors else "stop_tightened_old_cancel_pending",
        "previous_stop": current,
        "desired_stop": desired,
        "new_order": new_order,
        "cancelled_algo_ids": cancelled,
        "cancel_errors": cancel_errors,
    }


def tighten_position_stop_to_price(
    client: BinanceFuturesClient,
    position: dict[str, Any],
    desired_stop: float,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Use the atomic stop replacement path for a strategy-supplied absolute stop."""
    symbol = _position_symbol(position)
    entry = _entry_price(position)
    mark = _mark_price(position)
    amount = _position_amount(position)
    direction = "LONG" if amount > 0 else "SHORT"
    desired = float(desired_stop or 0)
    valid = desired < mark if direction == "LONG" else desired > mark
    if not symbol or entry <= 0 or mark <= 0 or desired <= 0 or not valid:
        return {
            "action": "trail_stop",
            "executed": False,
            "management_status": "strategy_stop_invalid",
            "desired_stop": desired,
            "mark": mark,
        }
    synthetic_atr = abs(mark - desired)
    action = {
        "action": "trail_stop",
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "mark": mark,
        "atr_pct": synthetic_atr / mark * 100,
    }
    tracked_item = {
        "trailing_distance_atr": 1.0,
        "last_stop_adjustment_at": None,
    }
    return _replace_dynamic_stop(
        client,
        ExchangeFilters(client.exchange_info()),
        position,
        action,
        tracked_item,
        config,
    )


def build_v432_add_on_plan(
    position: dict[str, Any],
    action: dict[str, Any],
    tracked_item: dict[str, Any],
    account: dict[str, Any],
    config: dict[str, Any],
    filters: ExchangeFilters,
    release_guard: dict[str, Any] | None = None,
    strategy_canary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one protected V4.3.2 add-on without changing exchange state."""
    symbol = _position_symbol(position)
    mark = float(action.get("mark") or _mark_price(position) or 0.0)
    stop = float(action.get("desired_stop") or tracked_item.get("managed_stop") or 0.0)
    strategy_version = str(tracked_item.get("strategy_version") or "")
    confidence = tracked_item.get("position_confidence") or {}
    release_guard = release_guard or {}
    strategy_canary = strategy_canary or {}
    blockers: list[str] = []
    if not config.get("opportunity_v432_add_on_enabled", True):
        blockers.append("add_on_disabled")
    if not config.get("opportunity_v432_add_on_live_enabled", True):
        blockers.append("add_on_live_disabled")
    if not strategy_version.startswith("v4.3.2"):
        blockers.append("strategy_version_mismatch")
    if not bool(confidence.get("add_on_eligible")):
        blockers.append("initial_opportunity_not_eligible")
    if tracked_item.get("add_on_attempted") or tracked_item.get("add_on_executed"):
        blockers.append("add_on_already_executed")
    if action.get("action") not in {"move_break_even", "trail_stop"} or not action.get("executed"):
        blockers.append("break_even_stop_not_confirmed")
    if not str(action.get("management_status") or "").startswith("stop_tightened"):
        blockers.append("full_stop_not_confirmed")
    if float(action.get("pnl_pct") or 0.0) < float(config.get("opportunity_v432_add_on_trigger_atr", 0.55)) * float(
        action.get("atr_pct") or 0.0
    ):
        blockers.append("favorable_move_insufficient")
    direction = str(action.get("direction") or "LONG")
    entry = float(action.get("entry") or _entry_price(position) or 0.0)
    buffer_pct = float(config.get("protection_break_even_buffer_pct", 0.08)) / 100
    break_even_with_cost = entry * (1 - buffer_pct) if direction == "SHORT" else entry * (1 + buffer_pct)
    stop_locks_cost = stop <= break_even_with_cost if direction == "SHORT" else stop >= break_even_with_cost
    if entry <= 0 or not stop_locks_cost:
        blockers.append("stop_not_at_break_even_plus_cost")
    fallback_drawdown = float(config.get("opportunity_v432_release_fallback_drawdown_pct", 8.0))
    release_drawdown = float(release_guard.get("drawdown_pct") or 0.0)
    if release_guard.get("fallback_active") or release_drawdown >= fallback_drawdown:
        blockers.append("release_drawdown_fallback")
    equity = float(account.get("equity") or 0.0)
    hard_stop = float(config.get("hard_stop_equity", 5.0))
    if equity <= hard_stop:
        blockers.append("hard_stop_equity")
    if mark <= 0 or stop <= 0 or abs(mark - stop) <= 0:
        blockers.append("price_or_stop_missing")

    initial_risk = max(0.0, float(tracked_item.get("initial_risk_pct") or 0.0))
    canary_expires = _parse_time(strategy_canary.get("expires_at"))
    startup_canary_active = bool(
        strategy_canary.get("permit_kind") == "release_startup"
        and canary_expires
        and _now() < canary_expires
    )
    canary_multiplier = 1.0
    if startup_canary_active:
        canary_multiplier = max(0.0, min(1.0, float(strategy_canary.get("risk_multiplier") or 0.0)))
        issued_at = _parse_time(strategy_canary.get("issued_at"))
        opened_at = _parse_time(tracked_item.get("opened_at"))
        if issued_at and opened_at and opened_at < issued_at:
            blockers.append("position_predates_startup_canary")
        if str(strategy_canary.get("status") or "") not in {"probe_open", "waiting_candidate"}:
            blockers.append("startup_canary_not_active_for_position")
    total_cap = min(
        15.0,
        max(0.0, float(config.get("opportunity_v432_add_on_total_risk_cap_pct", 15.0))),
    ) * canary_multiplier
    remaining_risk = max(0.0, total_cap - initial_risk)
    if remaining_risk < float(config.get("opportunity_v432_add_on_min_risk_budget_pct", 0.5)):
        blockers.append("risk_budget_exhausted")
    current_quantity = abs(_position_amount(position))
    initial_quantity = max(0.0, float(tracked_item.get("initial_quantity") or current_quantity))
    if current_quantity <= 0 or initial_quantity <= 0:
        blockers.append("position_quantity_missing")
    if blockers:
        return {
            "enabled": True,
            "allowed": False,
            "symbol": symbol,
            "blockers": blockers,
            "initial_risk_pct": round(initial_risk, 6),
            "remaining_risk_budget_pct": round(remaining_risk, 6),
            "total_risk_cap_pct": round(total_cap, 6),
            "startup_canary_multiplier": round(canary_multiplier, 6),
        }

    distance = abs(mark - stop)
    quantity_by_risk = equity * remaining_risk / 100 / distance
    quantity_by_initial = initial_quantity * float(config.get("opportunity_v432_add_on_max_initial_quantity_ratio", 0.75))
    leverage = max(1.0, float(tracked_item.get("leverage") or config.get("stage_s0_max_leverage", 5)))
    available = max(0.0, float(account.get("available_balance") or 0.0))
    quantity_by_margin = available * leverage * 0.90 / mark
    quantity = filters.quantity(symbol, min(quantity_by_risk, quantity_by_initial, quantity_by_margin))
    notional = quantity * mark
    min_notional = max(
        filters.min_notional(symbol),
        float(config.get("effective_min_order_notional_usdt", 10.0)),
    )
    if quantity <= 0 or notional < min_notional:
        blockers.append("below_minimum_notional")
    add_on_risk_pct = quantity * distance / equity * 100 if equity > 0 else 0.0
    return {
        "enabled": True,
        "allowed": not blockers,
        "symbol": symbol,
        "direction": direction,
        "quantity": quantity,
        "notional": round(notional, 8),
        "mark": mark,
        "confirmed_stop": stop,
        "initial_quantity": initial_quantity,
        "current_quantity": current_quantity,
        "initial_risk_pct": round(initial_risk, 6),
        "add_on_risk_pct": round(add_on_risk_pct, 6),
        "total_nominal_risk_pct": round(initial_risk + add_on_risk_pct, 6),
        "remaining_risk_budget_pct": round(remaining_risk, 6),
        "total_risk_cap_pct": round(total_cap, 6),
        "startup_canary_multiplier": round(canary_multiplier, 6),
        "release_drawdown_pct": round(release_drawdown, 6),
        "blockers": blockers,
    }


def _find_live_position(
    rows: list[dict[str, Any]],
    symbol: str,
    direction: str,
) -> dict[str, Any] | None:
    for row in rows:
        if _position_symbol(row) != symbol.upper() or abs(_position_amount(row)) <= 0:
            continue
        row_side = str(row.get("positionSide") or "").upper()
        row_direction = row_side if row_side in {"LONG", "SHORT"} else ("LONG" if _position_amount(row) > 0 else "SHORT")
        if row_direction == direction.upper():
            return row
    return None


def _execute_v432_add_on(
    client: BinanceFuturesClient,
    filters: ExchangeFilters,
    position: dict[str, Any],
    action: dict[str, Any],
    tracked_item: dict[str, Any],
    account: dict[str, Any],
    config: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    plan = build_v432_add_on_plan(
        position,
        action,
        tracked_item,
        account,
        config,
        filters,
        release_guard=state.get("strategy_release_equity_guard") or {},
        strategy_canary=state.get("strategy_canary") or {},
    )
    if not plan.get("allowed"):
        return plan
    symbol = str(plan["symbol"])
    direction = str(plan["direction"])
    quantity = float(plan["quantity"])
    confirmed_stop = float(plan.get("confirmed_stop") or 0.0)
    full_stop_confirmed = any(
        _algo_type(order) == "STOP_MARKET"
        and _algo_closes_position(order)
        and abs(_algo_trigger(order) - confirmed_stop) <= max(abs(confirmed_stop) * 0.000001, 0.00000001)
        for order in client.open_algo_orders(symbol)
    )
    if not full_stop_confirmed:
        return {
            **plan,
            "allowed": False,
            "executed": False,
            "status": "full_close_position_stop_not_confirmed",
            "blockers": [*(plan.get("blockers") or []), "full_close_position_stop_not_confirmed"],
        }
    entry_side = "BUY" if direction == "LONG" else "SELL"
    close_side = "SELL" if direction == "LONG" else "BUY"
    position_side = str(position.get("positionSide") or "").upper()
    if position_side not in {"LONG", "SHORT"}:
        position_side = None
    original_quantity = abs(_position_amount(position))
    tracked_item.update(
        {
            "add_on_attempted": True,
            "add_on_attempted_at": _now().isoformat(),
        }
    )
    runtime_positions = state.get("runtime_protection_positions")
    if isinstance(runtime_positions, dict) and any(item is tracked_item for item in runtime_positions.values()):
        # Persist the one-shot guard before sending the market order. If the
        # process or the follow-up position query fails, a restart cannot issue
        # the same add-on a second time.
        save_state({"runtime_protection_positions": runtime_positions})
    order = client.place_market_order(symbol, entry_side, quantity, position_side=position_side)
    refreshed_rows = client.position_risk()
    refreshed = _find_live_position(refreshed_rows, symbol, direction)
    refreshed_quantity = abs(_position_amount(refreshed or {}))
    filled_quantity = max(0.0, refreshed_quantity - original_quantity)
    if filled_quantity <= 0:
        try:
            filled_quantity = float(order.get("executedQty") or order.get("origQty") or 0.0)
        except (AttributeError, TypeError, ValueError):
            filled_quantity = 0.0
    filled_quantity = filters.quantity(symbol, filled_quantity)
    if refreshed is not None:
        refreshed = enrich_positions_with_prices([refreshed], refreshed_rows)[0]
    audit = (
        audit_position_protection(client, refreshed, config, filters=filters, repair=True)
        if refreshed is not None
        else {"protected": False, "status": "position_refresh_missing"}
    )
    protected = bool(audit.get("protected") or audit.get("repair_status") == "repaired")
    if filled_quantity <= 0:
        tracked_item.update(
            {
                "add_on_executed": True,
                "add_on_executed_at": _now().isoformat(),
                "add_on_fill_unconfirmed": True,
                "add_on_quantity": 0.0,
                "add_on_risk_pct": plan.get("add_on_risk_pct"),
                "total_nominal_risk_pct": plan.get("total_nominal_risk_pct"),
            }
        )
        result = {
            **plan,
            "allowed": False,
            "executed": False,
            "status": "add_on_fill_unconfirmed_repeat_blocked",
            "blockers": [*(plan.get("blockers") or []), "add_on_fill_unconfirmed_repeat_blocked"],
            "order": order,
            "filled_quantity": 0.0,
            "protection_audit": audit,
        }
        record_event("error", "v432_add_on", "追加仓位成交量无法确认，已锁定本次追加资格防止重复加仓", result)
        return result
    if not protected:
        rollback = None
        if filled_quantity > 0:
            rollback = client.place_market_order(
                symbol,
                close_side,
                filled_quantity,
                reduce_only=position_side is None,
                position_side=position_side,
            )
        result = {
            **plan,
            "allowed": False,
            "executed": False,
            "status": "protection_failed_add_on_rolled_back",
            "order": order,
            "filled_quantity": filled_quantity,
            "protection_audit": audit,
            "rollback_order": rollback,
        }
        record_event("error", "v432_add_on", "追加仓位保护确认失败，已仅撤回新增数量", result)
        return result
    tracked_item.update(
        {
            "add_on_executed": True,
            "add_on_executed_at": _now().isoformat(),
            "add_on_quantity": filled_quantity,
            "add_on_risk_pct": plan.get("add_on_risk_pct"),
            "total_nominal_risk_pct": plan.get("total_nominal_risk_pct"),
        }
    )
    result = {
        **plan,
        "executed": True,
        "status": "protected_add_on_executed",
        "order": order,
        "filled_quantity": filled_quantity,
        "protection_audit": audit,
    }
    record_event("warning", "v432_add_on", "V4.3.2 保本保护确认后完成一次受限追加", result)
    return result


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
    daily_strategy_managed = (
        tracked_item.get("protection_version") == "market_tsmom_daily_v2"
        and tracked_item.get("runtime_intraday_trailing_enabled") is False
    )
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
    atr_value = float(tracked_item.get("entry_atr") or 0.0)
    atr_source = "entry_snapshot" if atr_value > 0 else "missing"
    if atr_value <= 0 and client is not None:
        try:
            atr_value = _atr_value(client, symbol, interval)
            atr_source = "rest_kline"
        except Exception as exc:
            record_event("warning", "runtime_protection", f"ATR check failed for {symbol}: {exc}")
    atr_pct = atr_value / mark * 100 if mark > 0 and atr_value > 0 else 0.0
    fast_invalid_pct = atr_pct * float(config.get("protection_fast_invalid_atr", 0.35))
    fast_invalid_seconds = int(
        tracked_item.get("fast_invalid_seconds")
        or config.get("protection_fast_invalid_seconds", 90)
    )
    max_hold_seconds = int((tracked.get(key) or {}).get("max_hold_seconds") or 0)
    stagnation_seconds = int(tracked_item.get("stagnation_seconds") or 0)
    stagnation_min_profit_pct = float(
        tracked_item.get("stagnation_min_profit_pct")
        or config.get("protection_min_profit_after_cost_pct", 0.08)
    )
    max_hold_bars = int((tracked.get(key) or {}).get("max_hold_bars") or config.get("runtime_protection_max_hold_bars", 12))
    if max_hold_seconds <= 0:
        max_hold_seconds = max_hold_bars * 300
    break_even_trigger_pct = atr_pct * float(tracked_item.get("break_even_atr") or config.get("protection_break_even_trigger_atr", 0.55))
    trailing_trigger_pct = atr_pct * float(tracked_item.get("trailing_trigger_atr") or config.get("protection_trailing_trigger_atr", 0.9))
    trailing_distance_pct = atr_pct * float(tracked_item.get("trailing_distance_atr") or config.get("protection_trailing_distance_atr", 0.55))
    action = "observe"
    reason = "holding"
    orderbook_exit = {"enabled": False}
    if daily_strategy_managed:
        return {
            "symbol": symbol,
            "direction": direction,
            "quantity": abs(amount),
            "entry": entry,
            "mark": mark,
            "pnl_pct": round(pnl_pct, 6),
            "age_seconds": round(age_seconds, 3),
            "atr_pct": round(atr_pct, 6),
            "atr_source": atr_source,
            "price_source": position.get("mark_price_source") or "account",
            "max_hold_seconds": max_hold_seconds,
            "trailing_distance_pct": 0.0,
            "orderbook_exit": orderbook_exit,
            "action": "observe",
            "reason": "daily_strategy_managed",
        }
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
    elif stagnation_seconds > 0 and age_seconds >= stagnation_seconds and pnl_pct < stagnation_min_profit_pct:
        action = "close_stagnation"
        reason = "stagnation_after_cost"
    elif (
        max_hold_seconds > 0
        and age_seconds >= max_hold_seconds
        and pnl_pct < max(stagnation_min_profit_pct, trailing_trigger_pct)
    ):
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
        "atr_source": atr_source,
        "price_source": position.get("mark_price_source") or "account",
        "fast_invalid_pct": round(fast_invalid_pct, 6),
        "stagnation_seconds": stagnation_seconds,
        "stagnation_min_profit_pct": round(stagnation_min_profit_pct, 6),
        "max_hold_seconds": max_hold_seconds,
        "trailing_distance_pct": round(trailing_distance_pct, 6),
        "orderbook_exit": orderbook_exit,
        "action": action,
        "reason": reason,
    }


def _manage_runtime_protection(
    client: BinanceFuturesClient,
    config: dict[str, Any],
    state: dict[str, Any],
    account: dict[str, Any],
) -> dict[str, Any]:
    if not config.get("dynamic_protection_runtime_enabled", True):
        return {"enabled": False, "actions": []}
    positions = [item for item in account.get("positions", []) if abs(_position_amount(item)) > 0]
    missing_price_symbols: set[str] = set()
    enriched_positions: list[dict[str, Any]] = []
    stream_max_age = int(config.get("runtime_protection_stream_price_max_age_seconds", 10))
    for position in positions:
        enriched = dict(position)
        symbol = _position_symbol(enriched)
        ticker = stream_ticker(symbol, max_age_seconds=stream_max_age)
        stream_price = float((ticker or {}).get("lastPrice") or 0.0)
        if stream_price > 0:
            enriched["markPrice"] = str(stream_price)
            enriched["mark_price_source"] = "websocket"
        elif _mark_price(enriched) <= 0:
            missing_price_symbols.add(symbol)
        enriched_positions.append(enriched)
    positions = enriched_positions
    if missing_price_symbols and client is not None:
        try:
            positions = enrich_positions_with_prices(positions, client.position_risk())
            for position in positions:
                if _position_symbol(position) in missing_price_symbols:
                    position["mark_price_source"] = "positionRisk"
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
        tracked_item = tracked.get(key) or {}
        if (
            action["action"] in {"move_break_even", "trail_stop"}
            and config.get("runtime_stop_management_enabled", True)
            and tracked_item.get("protection_version") == "v5_dynamic"
        ):
            if filters is None:
                filters = ExchangeFilters(client.exchange_info())
            try:
                action = _replace_dynamic_stop(client, filters, position, action, tracked_item, config)
            except Exception as exc:
                action = {**action, "executed": False, "management_status": "stop_management_error", "error": str(exc)}
                record_event_throttled(
                    "warning",
                    "runtime_protection",
                    f"dynamic stop management failed for {symbol}",
                    action,
                    throttle_seconds=60,
                )
        if (
            action.get("action") in {"move_break_even", "trail_stop"}
            and action.get("executed")
            and live_trading_allowed(config)
            and config.get("opportunity_v432_add_on_enabled", True)
        ):
            if filters is None:
                filters = ExchangeFilters(client.exchange_info())
            try:
                action["add_on"] = _execute_v432_add_on(
                    client,
                    filters,
                    position,
                    action,
                    tracked_item,
                    account,
                    config,
                    state,
                )
            except Exception as exc:
                action["add_on"] = {
                    "enabled": True,
                    "allowed": False,
                    "executed": False,
                    "status": "add_on_error",
                    "error": str(exc),
                }
                record_event_throttled(
                    "error",
                    "v432_add_on",
                    f"V4.3.2 受保护追加失败：{symbol}",
                    action["add_on"],
                    throttle_seconds=60,
                )
        actions.append(action)
        if action["action"] not in {
            "close_fast_invalid",
            "close_stagnation",
            "close_time_stop",
            "close_orderbook_invalid",
        }:
            continue
        v44_runtime_exit = bool(
            strategy_supports(tracked_item.get("strategy_version"), "full_bet")
            and config.get("opportunity_v44_runtime_exit_enabled", True)
        )
        if not (
            live_trading_allowed(config)
            and (config.get("dynamic_protection_runtime_trade_enabled", False) or v44_runtime_exit)
        ):
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
        close_order = client.place_market_order(
            symbol,
            close_side,
            quantity,
            reduce_only=position_side is None,
            position_side=position_side,
        )
        cleanup_error = None
        try:
            client.cancel_all_open_algo_orders(symbol)
        except Exception as exc:
            cleanup_error = str(exc)
            record_event_throttled(
                "warning",
                "runtime_protection",
                f"position closed but stale protection cleanup failed for {symbol}",
                {"symbol": symbol, "error": cleanup_error},
                throttle_seconds=60,
            )
        action["executed"] = True
        action["close_order"] = close_order
        action["cleanup_error"] = cleanup_error
        record_event("warning", "runtime_protection", "runtime protection closed position", action)
    tracked = _merge_active_tracking_with_latest(tracked, active_keys)
    updates["runtime_protection_positions"] = tracked
    save_state(updates)
    return {"enabled": True, "actions": actions}


def manage_runtime_protection(
    client: BinanceFuturesClient,
    config: dict[str, Any],
    state: dict[str, Any],
    account: dict[str, Any],
) -> dict[str, Any]:
    # Position price recovery, stop inspection and emergency exits are safety
    # work. They must retain REST capacity even when called by the background scan.
    with request_priority("critical"):
        return _manage_runtime_protection(client, config, state, account)
