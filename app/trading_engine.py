from __future__ import annotations

from typing import Any

from app.binance_client import BinanceFuturesClient
from app.exchange_filters import ExchangeFilters
from app.grid import build_grid_orders, build_grid_plan
from app.risk import assess_new_position, current_stage, live_trading_allowed, position_size_from_risk
from app.scanner import latest_strategy_signal, mode_config, scan_growth_candidates
from app.state_store import save_state
from app.strategy import StrategyParams


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
    scan_candidate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    active_mode = mode_config(config, account_summary.get("equity"))
    if scan_candidate:
        active_mode = {
            **active_mode,
            "mode": scan_candidate.get("mode", active_mode["mode"]),
            "strategy": scan_candidate.get("strategy", active_mode["strategy"]),
            "risk_pct": scan_candidate.get("risk_pct", active_mode["risk_pct"]),
            "leverage": scan_candidate.get("leverage", active_mode["leverage"]),
            "margin_pct": scan_candidate.get("margin_pct", active_mode["margin_pct"]),
        }
    direction = str((scan_candidate or {}).get("direction", "LONG")).upper()
    signal = latest_strategy_signal(symbol, bars, active_mode["strategy"], StrategyParams(), direction=direction)
    equity = account_summary.get("equity")
    if signal.get("signal") not in {"LONG", "SHORT"} or signal.get("signal") != direction:
        return {"symbol": symbol, "action": "WAIT", "signal": signal, "risk": {"allowed": False, "reason": "no_signal"}}
    if equity is None:
        return {"symbol": symbol, "action": "WAIT", "signal": signal, "risk": {"allowed": False, "reason": "account_unavailable"}}

    daily_loss_key = "daily_loss_limit_pct"
    if active_mode["mode"] == "attack":
        daily_loss_key = "attack_daily_loss_limit_pct"
    if active_mode["mode"] == "tournament":
        daily_loss_key = "tournament_daily_loss_limit_pct"
    risk = assess_new_position(
        config,
        state,
        equity,
        symbol,
        account_summary.get("positions", []),
        overrides={
            "margin_pct": active_mode["margin_pct"],
            "leverage": active_mode["leverage"],
            "daily_loss_limit_pct": config.get(daily_loss_key, config.get("daily_loss_limit_pct", 3.0)),
        },
    )
    quantity = position_size_from_risk(
        equity=equity,
        risk_pct=float(active_mode["risk_pct"]),
        entry=float(signal["last_price"]),
        stop=float(signal["stop"]),
    )
    max_qty = risk.max_notional / float(signal["last_price"]) if signal.get("last_price") else 0
    quantity = min(quantity, max_qty)
    return {
        "symbol": symbol,
        "action": f"OPEN_{direction}" if risk.allowed and quantity > 0 else "WAIT",
        "direction": direction,
        "signal": signal,
        "risk": risk.__dict__,
        "quantity": quantity,
        "estimated_notional": quantity * float(signal["last_price"]),
        "mode": active_mode["mode"],
        "strategy": active_mode["strategy"],
        "risk_pct": active_mode["risk_pct"],
        "leverage": active_mode["leverage"],
    }


def build_best_growth_decision(
    client: BinanceFuturesClient,
    config: dict[str, Any],
    state: dict[str, Any],
    account_summary: dict[str, Any],
) -> dict[str, Any]:
    scan = scan_growth_candidates(client, config, account_summary)
    best = next((item for item in scan["candidates"] if item.get("passed")), None)
    if not best:
        return {
            "action": "WAIT",
            "reason": "no_candidate_passed",
            "scan": scan,
            "risk": {"allowed": False, "reason": "no_candidate_passed"},
        }
    bars = client.klines_history(best["symbol"], scan["mode"]["interval"], int(scan["mode"]["recent_days"]))
    decision = build_stage1_decision(best["symbol"], bars, config, state, account_summary, scan_candidate=best)
    decision["scan"] = scan
    decision["candidate"] = best
    return decision


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
    if decision.get("action") not in {"OPEN_LONG", "OPEN_SHORT"}:
        return {"mode": "none", "message": "No executable decision."}
    filters = ExchangeFilters(client.exchange_info())
    symbol = decision["symbol"]
    quantity = filters.quantity(symbol, float(decision["quantity"]))
    stop = filters.price(symbol, float(decision["signal"]["stop"]))
    take_profit = filters.price(symbol, float(decision["signal"]["take_profit"]))
    notional = quantity * float(decision["signal"]["last_price"])
    min_notional = filters.min_notional(symbol)
    direction = str(decision.get("direction") or decision.get("signal", {}).get("signal") or "LONG").upper()
    entry_side = "SELL" if direction == "SHORT" else "BUY"
    close_side = "BUY" if direction == "SHORT" else "SELL"
    order = {
        "symbol": symbol,
        "side": entry_side,
        "direction": direction,
        "quantity": quantity,
        "stop": stop,
        "take_profit": take_profit,
        "notional": notional,
    }
    if quantity <= 0 or notional < min_notional:
        return {"mode": "blocked", "message": "Quantity is below exchange minimum.", "order": order}
    if not live_trading_allowed(config):
        return {"mode": "dry_run", "order": order}
    leverage = max(1, min(50, int(float(decision.get("leverage", config.get("stage1_max_leverage", 2))))))
    client.set_leverage(symbol, leverage)
    entry_order = client.place_market_order(symbol=symbol, side=entry_side, quantity=quantity)
    stop_order = client.place_stop_market(symbol=symbol, side=close_side, stop_price=stop)
    take_profit_order = client.place_take_profit_market(symbol=symbol, side=close_side, stop_price=take_profit)
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
