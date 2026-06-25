from __future__ import annotations

from typing import Any

from app.strategy import StrategyParams, atr


def build_grid_plan(symbol: str, bars: list[list[Any]], config: dict[str, Any], equity: float) -> dict[str, Any]:
    if len(bars) < 60:
        return {"symbol": symbol, "status": "WAIT", "reason": "not_enough_data"}

    closes = [float(bar[4]) for bar in bars]
    last = closes[-1]
    atr_value = atr(bars, StrategyParams().atr_period)[-1]
    atr_pct = atr_value / last

    # Grid is only for moderate volatility. Too quiet earns little; too violent can break ranges fast.
    if atr_pct < 0.003:
        return {"symbol": symbol, "status": "WAIT", "reason": "volatility_too_low", "atr_pct": atr_pct}
    if atr_pct > 0.035:
        return {"symbol": symbol, "status": "WAIT", "reason": "volatility_too_high", "atr_pct": atr_pct}

    width = max(atr_value * 6, last * 0.06)
    lower = last - width
    upper = last + width
    min_levels = int(config.get("grid_min_levels", 20))
    max_levels = int(config.get("grid_max_levels", 80))
    raw_levels = int((upper - lower) / max(atr_value * 0.35, last * 0.002))
    levels = min(max(raw_levels, min_levels), max_levels)
    reserve_pct = float(config.get("grid_reserve_cash_pct", 25.0))
    deploy_equity = equity * (100 - reserve_pct) / 100
    per_grid_notional = deploy_equity / max(levels, 1)

    return {
        "symbol": symbol,
        "status": "READY",
        "last_price": last,
        "lower": lower,
        "upper": upper,
        "levels": levels,
        "atr": atr_value,
        "atr_pct": atr_pct,
        "deploy_equity": deploy_equity,
        "per_grid_notional": per_grid_notional,
        "leverage": float(config.get("stage2_max_leverage", 1.5)),
        "stop_on_breakout": bool(config.get("grid_stop_on_breakout", True)),
    }


def build_grid_orders(plan: dict[str, Any], config: dict[str, Any], position_amount: float = 0.0) -> list[dict[str, Any]]:
    if plan.get("status") != "READY":
        return []

    levels = int(plan["levels"])
    half = max(1, levels // 2)
    last = float(plan["last_price"])
    lower = float(plan["lower"])
    upper = float(plan["upper"])
    per_grid_notional = float(plan["per_grid_notional"])
    allow_short = bool(config.get("allow_short", False))
    orders: list[dict[str, Any]] = []

    for index in range(1, half + 1):
        price = last - (last - lower) * index / half
        orders.append({
            "side": "BUY",
            "price": price,
            "quantity": per_grid_notional / price,
            "reduce_only": False,
        })

    if allow_short or position_amount > 0:
        for index in range(1, half + 1):
            price = last + (upper - last) * index / half
            orders.append({
                "side": "SELL",
                "price": price,
                "quantity": per_grid_notional / price,
                "reduce_only": not allow_short,
            })

    return orders
