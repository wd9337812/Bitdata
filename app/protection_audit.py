from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.binance_client import BinanceFuturesClient
from app.exchange_filters import ExchangeFilters
from app.market_stream import stream_ticker
from app.telemetry import record_event_throttled


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _position_amount(position: dict[str, Any]) -> float:
    return _float(position.get("positionAmt") or position.get("position_amt"))


def _position_direction(position: dict[str, Any]) -> str:
    side = str(position.get("positionSide") or "").upper()
    if side in {"LONG", "SHORT"}:
        return side
    return "SHORT" if _position_amount(position) < 0 else "LONG"


def _position_symbol(position: dict[str, Any]) -> str:
    return str(position.get("symbol") or "").upper()


def _position_entry(position: dict[str, Any]) -> float:
    return _float(position.get("entryPrice") or position.get("entry_price"))


def _position_key(symbol: str, direction: str) -> str:
    return f"{symbol.upper()}:{direction.upper()}"


def _find_position_risk(
    position_risk_rows: list[dict[str, Any]],
    symbol: str,
    direction: str,
) -> dict[str, Any] | None:
    symbol = symbol.upper()
    direction = direction.upper()
    candidates = [
        item for item in position_risk_rows
        if str(item.get("symbol") or "").upper() == symbol
    ]
    for item in candidates:
        side = str(item.get("positionSide") or "").upper()
        amount = _float(item.get("positionAmt"))
        if side == direction and abs(amount) > 0:
            return item
    for item in candidates:
        amount = _float(item.get("positionAmt"))
        if direction == ("SHORT" if amount < 0 else "LONG") and abs(amount) > 0:
            return item
    return None


def enrich_positions_with_prices(
    positions: list[dict[str, Any]],
    position_risk_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Fill mark price fields using positionRisk first, then local WebSocket ticker."""
    position_risk_rows = position_risk_rows or []
    enriched: list[dict[str, Any]] = []
    for raw in positions:
        position = dict(raw)
        symbol = _position_symbol(position)
        direction = _position_direction(position)
        risk = _find_position_risk(position_risk_rows, symbol, direction)
        if risk:
            for key in [
                "markPrice",
                "unRealizedProfit",
                "liquidationPrice",
                "notional",
                "breakEvenPrice",
                "updateTime",
            ]:
                if risk.get(key) not in {None, ""}:
                    position[key] = risk[key]
            if not _position_entry(position) and risk.get("entryPrice"):
                position["entryPrice"] = risk["entryPrice"]
        if _float(position.get("markPrice")) <= 0:
            ticker = stream_ticker(symbol, max_age_seconds=20)
            if ticker:
                position["markPrice"] = ticker.get("lastPrice") or ticker.get("last_price")
                position["price_source"] = "websocket"
        elif risk:
            position["price_source"] = "positionRisk"
        enriched.append(position)
    return enriched


def _trigger_price(order: dict[str, Any]) -> float:
    return _float(order.get("triggerPrice") or order.get("stopPrice") or order.get("price"))


def _matching_algo_orders(
    orders: list[dict[str, Any]],
    *,
    symbol: str,
    direction: str,
) -> dict[str, list[dict[str, Any]]]:
    close_side = "SELL" if direction == "LONG" else "BUY"
    result = {"STOP_MARKET": [], "TAKE_PROFIT_MARKET": []}
    for order in orders:
        if str(order.get("symbol") or "").upper() != symbol.upper():
            continue
        if str(order.get("side") or "").upper() != close_side:
            continue
        position_side = str(order.get("positionSide") or direction).upper()
        if position_side not in {"BOTH", direction.upper()}:
            continue
        status = str(order.get("algoStatus") or order.get("status") or "").upper()
        if status and status not in {"NEW", "PARTIALLY_FILLED"}:
            continue
        order_type = str(order.get("orderType") or order.get("type") or "").upper()
        if order_type in result:
            result[order_type].append(order)
    return result


def _valid_stop_price(trigger: float, mark: float, direction: str) -> bool:
    if trigger <= 0 or mark <= 0:
        return False
    return trigger < mark if direction == "LONG" else trigger > mark


def _valid_take_profit_price(trigger: float, mark: float, direction: str) -> bool:
    if trigger <= 0 or mark <= 0:
        return False
    return trigger > mark if direction == "LONG" else trigger < mark


def build_missing_protection_plan(
    position: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    symbol = _position_symbol(position)
    direction = _position_direction(position)
    entry = _position_entry(position)
    mark = _float(position.get("markPrice") or position.get("last_price"))
    price = mark or entry
    if price <= 0:
        return {"enabled": False, "reason": "missing_price", "symbol": symbol, "direction": direction}
    stop_pct = float(config.get("protection_audit_fallback_stop_pct", 0.9))
    take_profit_pct = float(config.get("protection_audit_fallback_take_profit_pct", 1.2))
    if direction == "SHORT":
        stop = price * (1 + stop_pct / 100)
        take_profit = price * (1 - take_profit_pct / 100)
    else:
        stop = price * (1 - stop_pct / 100)
        take_profit = price * (1 + take_profit_pct / 100)
    return {
        "enabled": True,
        "reason": "fallback_percent",
        "symbol": symbol,
        "direction": direction,
        "reference_price": price,
        "stop": stop,
        "take_profit": take_profit,
    }


def audit_position_protection(
    client: BinanceFuturesClient,
    position: dict[str, Any],
    config: dict[str, Any],
    *,
    algo_orders: list[dict[str, Any]] | None = None,
    filters: ExchangeFilters | None = None,
    repair: bool = True,
) -> dict[str, Any]:
    symbol = _position_symbol(position)
    direction = _position_direction(position)
    mark = _float(position.get("markPrice") or position.get("last_price"))
    if not symbol or abs(_position_amount(position)) <= 0:
        return {"symbol": symbol, "direction": direction, "status": "no_position", "protected": True}
    if algo_orders is None:
        algo_orders = client.open_algo_orders(symbol)
    matched = _matching_algo_orders(algo_orders, symbol=symbol, direction=direction)
    stop_orders = [order for order in matched["STOP_MARKET"] if _valid_stop_price(_trigger_price(order), mark, direction)]
    take_profit_orders = [
        order for order in matched["TAKE_PROFIT_MARKET"]
        if _valid_take_profit_price(_trigger_price(order), mark, direction)
    ]
    invalid_stop_orders = len(matched["STOP_MARKET"]) - len(stop_orders)
    invalid_take_profit_orders = len(matched["TAKE_PROFIT_MARKET"]) - len(take_profit_orders)
    missing = []
    if not stop_orders:
        missing.append("stop")
    if not take_profit_orders:
        missing.append("take_profit")
    if (
        repair
        and missing
        and config.get("protection_audit_rebuild_invalid_enabled", True)
        and (invalid_stop_orders > 0 or invalid_take_profit_orders > 0)
    ):
        client.cancel_all_open_algo_orders(symbol)
        stop_orders = []
        take_profit_orders = []
        missing = ["stop", "take_profit"]
    status = "protected" if not missing else "missing_" + "_".join(missing)
    result: dict[str, Any] = {
        "symbol": symbol,
        "direction": direction,
        "mark": mark,
        "status": status,
        "protected": not missing,
        "missing": missing,
        "stop_count": len(stop_orders),
        "take_profit_count": len(take_profit_orders),
        "invalid_stop_count": invalid_stop_orders,
        "invalid_take_profit_count": invalid_take_profit_orders,
    }
    if not missing or not repair or not config.get("protection_audit_auto_repair_enabled", True):
        return result
    plan = build_missing_protection_plan(position, config)
    result["repair_plan"] = plan
    if not plan.get("enabled"):
        result["repair_status"] = "skipped"
        return result
    filters = filters or ExchangeFilters(client.exchange_info())
    close_side = "SELL" if direction == "LONG" else "BUY"
    position_side = None
    try:
        if client.position_side_dual().get("dualSidePosition") is True:
            position_side = direction
    except Exception:
        position_side = None
    repaired: list[str] = []
    if "stop" in missing:
        stop_price = filters.price(symbol, float(plan["stop"]))
        order = client.place_algo_order(
            symbol=symbol,
            side=close_side,
            order_type="STOP_MARKET",
            trigger_price=stop_price,
            position_side=position_side,
        )
        result["stop_repair_order"] = order
        repaired.append("stop")
    if "take_profit" in missing:
        take_profit_price = filters.price(symbol, float(plan["take_profit"]))
        order = client.place_algo_order(
            symbol=symbol,
            side=close_side,
            order_type="TAKE_PROFIT_MARKET",
            trigger_price=take_profit_price,
            position_side=position_side,
        )
        result["take_profit_repair_order"] = order
        repaired.append("take_profit")
    result["repair_status"] = "repaired"
    result["repaired"] = repaired
    return result


def audit_account_protection(
    client: BinanceFuturesClient,
    config: dict[str, Any],
    account: dict[str, Any],
    *,
    repair: bool = True,
) -> dict[str, Any]:
    if not config.get("protection_audit_enabled", True):
        return {"enabled": False, "positions": []}
    positions = [item for item in account.get("positions", []) if abs(_position_amount(item)) > 0]
    if not positions:
        return {"enabled": True, "positions": [], "protected": True}
    position_risk_rows: list[dict[str, Any]] = []
    try:
        position_risk_rows = client.position_risk()
    except Exception as exc:
        record_event_throttled(
            "warning",
            "protection_audit",
            "positionRisk unavailable",
            {"error": str(exc)},
            throttle_seconds=120,
        )
    enriched = enrich_positions_with_prices(positions, position_risk_rows)
    filters = None
    results = []
    for position in enriched:
        symbol = _position_symbol(position)
        try:
            orders = client.open_algo_orders(symbol)
            result = audit_position_protection(
                client,
                position,
                config,
                algo_orders=orders,
                filters=filters,
                repair=repair,
            )
            if result.get("repair_status") == "repaired" and filters is None:
                filters = ExchangeFilters(client.exchange_info())
        except Exception as exc:
            result = {
                "symbol": symbol,
                "direction": _position_direction(position),
                "protected": False,
                "status": "audit_error",
                "error": str(exc),
            }
        results.append(result)
        if not result.get("protected"):
            record_event_throttled(
                "warning" if result.get("repair_status") != "repaired" else "info",
                "protection_audit",
                f"protection audit {result.get('status')}",
                result,
                throttle_seconds=int(config.get("protection_audit_log_throttle_seconds", 60)),
                key=f"{symbol}:{result.get('status')}:{result.get('repair_status')}",
            )
    protected = all(item.get("protected") or item.get("repair_status") == "repaired" for item in results)
    return {
        "enabled": True,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "protected": protected,
        "positions": results,
    }
