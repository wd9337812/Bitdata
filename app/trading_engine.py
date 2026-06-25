from __future__ import annotations

from typing import Any

from app.binance_client import BinanceFuturesClient
from app.exchange_filters import ExchangeFilters
from app.grid import build_grid_orders, build_grid_plan
from app.risk import assess_new_position, current_stage, live_trading_allowed, position_size_from_risk
from app.state_store import save_state
from app.strategy import StrategyParams, latest_signal


def summarize_account(account: dict[str, Any] | None) -> dict[str, Any]:
    if not account:
        return {"equity": None, "available_balance": None, "unrealized_pnl": None, "positions": []}
    positions = [
        pos for pos in account.get("positions", [])
        if abs(float(pos.get("positionAmt", 0))) > 0
    ]
    return {
        "equity": float(account.get("totalWalletBalance", 0)) + float(account.get("totalUnrealizedProfit", 0)),
        "available_balance": float(account.get("availableBalance", 0)),
        "unrealized_pnl": float(account.get("totalUnrealizedProfit", 0)),
        "positions": positions,
    }


def sync_stage(config: dict[str, Any], state: dict[str, Any], account_summary: dict[str, Any]) -> dict[str, Any]:
    equity = account_summary.get("equity")
    updates: dict[str, Any] = {}
    if equity is not None:
        updates["equity_high_watermark"] = max(float(state.get("equity_high_watermark") or 0), float(equity))
        if not state.get("daily_start_equity"):
            updates["daily_start_equity"] = float(equity)
    stage = current_stage(config, state, equity)
    updates["stage"] = stage
    return save_state(updates) if updates else state


def build_stage1_decision(
    symbol: str,
    bars: list[list[Any]],
    config: dict[str, Any],
    state: dict[str, Any],
    account_summary: dict[str, Any],
) -> dict[str, Any]:
    signal = latest_signal(symbol, bars, StrategyParams())
    equity = account_summary.get("equity")
    if signal.get("signal") != "LONG":
        return {"symbol": symbol, "action": "WAIT", "signal": signal, "risk": {"allowed": False, "reason": "no_signal"}}
    if equity is None:
        return {"symbol": symbol, "action": "WAIT", "signal": signal, "risk": {"allowed": False, "reason": "account_unavailable"}}

    risk = assess_new_position(config, state, equity, symbol, account_summary.get("positions", []))
    quantity = position_size_from_risk(
        equity=equity,
        risk_pct=float(config.get("risk_per_trade_pct", 1.0)),
        entry=float(signal["last_price"]),
        stop=float(signal["stop"]),
    )
    max_qty = risk.max_notional / float(signal["last_price"]) if signal.get("last_price") else 0
    quantity = min(quantity, max_qty)
    return {
        "symbol": symbol,
        "action": "OPEN_LONG" if risk.allowed and quantity > 0 else "WAIT",
        "signal": signal,
        "risk": risk.__dict__,
        "quantity": quantity,
        "estimated_notional": quantity * float(signal["last_price"]),
    }


def build_grid_decisions(config: dict[str, Any], account_summary: dict[str, Any], klines: dict[str, list[list[Any]]]) -> list[dict[str, Any]]:
    equity = account_summary.get("equity") or 0
    if equity <= 0:
        return [{"status": "WAIT", "reason": "account_unavailable"}]
    return [
        build_grid_plan(symbol, bars, config, equity)
        for symbol, bars in klines.items()
    ]


def execute_stage1_market_order(
    client: BinanceFuturesClient,
    decision: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    if decision.get("action") != "OPEN_LONG":
        return {"mode": "none", "message": "No executable decision."}
    filters = ExchangeFilters(client.exchange_info())
    symbol = decision["symbol"]
    quantity = filters.quantity(symbol, float(decision["quantity"]))
    stop = filters.price(symbol, float(decision["signal"]["stop"]))
    take_profit = filters.price(symbol, float(decision["signal"]["take_profit"]))
    notional = quantity * float(decision["signal"]["last_price"])
    min_notional = filters.min_notional(symbol)
    order = {
        "symbol": symbol,
        "side": "BUY",
        "quantity": quantity,
        "stop": stop,
        "take_profit": take_profit,
        "notional": notional,
    }
    if quantity <= 0 or notional < min_notional:
        return {"mode": "blocked", "message": "Quantity is below exchange minimum.", "order": order}
    if not live_trading_allowed(config):
        return {"mode": "dry_run", "order": order}
    leverage = max(1, min(10, int(float(config.get("stage1_max_leverage", 2)))))
    client.set_leverage(symbol, leverage)
    entry_order = client.place_market_order(symbol=symbol, side="BUY", quantity=quantity)
    stop_order = client.place_stop_market(symbol=symbol, side="SELL", stop_price=stop)
    take_profit_order = client.place_take_profit_market(symbol=symbol, side="SELL", stop_price=take_profit)
    return {
        "mode": "live",
        "entry_order": entry_order,
        "stop_order": stop_order,
        "take_profit_order": take_profit_order,
    }


def execute_grid_orders(
    client: BinanceFuturesClient,
    plan: dict[str, Any],
    config: dict[str, Any],
    position_amount: float = 0.0,
) -> dict[str, Any]:
    if plan.get("status") != "READY":
        return {"mode": "none", "message": "Grid plan is not ready.", "plan": plan}

    filters = ExchangeFilters(client.exchange_info())
    symbol = plan["symbol"]
    raw_orders = build_grid_orders(plan, config, position_amount=position_amount)
    orders = []
    for raw in raw_orders:
        price = filters.price(symbol, float(raw["price"]))
        quantity = filters.quantity(symbol, float(raw["quantity"]))
        if quantity * price >= filters.min_notional(symbol):
            orders.append({**raw, "price": price, "quantity": quantity})

    if not live_trading_allowed(config):
        return {"mode": "dry_run", "symbol": symbol, "orders": orders, "count": len(orders)}

    leverage = max(1, min(5, int(float(config.get("stage2_max_leverage", 1.5)))))
    client.set_leverage(symbol, leverage)
    client.cancel_all_open_orders(symbol)
    placed = [
        client.place_limit_order(
            symbol=symbol,
            side=order["side"],
            quantity=order["quantity"],
            price=order["price"],
            reduce_only=bool(order["reduce_only"]),
        )
        for order in orders
    ]
    return {"mode": "live", "symbol": symbol, "placed": placed, "count": len(placed)}
