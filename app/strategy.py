from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StrategyParams:
    ema_fast: int = 20
    ema_slow: int = 50
    atr_period: int = 14
    stop_atr: float = 1.0
    take_profit_atr: float = 1.5
    max_hold_bars: int = 18
    min_atr_pct: float = 0.004
    taker_fee: float = 0.0004


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out: list[float] = []
    current = values[0]
    for value in values:
        current = value * k + current * (1 - k)
        out.append(current)
    return out


def atr(bars: list[list[Any]], period: int) -> list[float]:
    out: list[float] = []
    trs: list[float] = []
    previous_close: float | None = None
    for bar in bars:
        high = float(bar[2])
        low = float(bar[3])
        close = float(bar[4])
        true_range = high - low
        if previous_close is not None:
            true_range = max(true_range, abs(high - previous_close), abs(low - previous_close))
        trs.append(true_range)
        window = trs[-period:]
        out.append(sum(window) / len(window))
        previous_close = close
    return out


def latest_signal(symbol: str, bars: list[list[Any]], params: StrategyParams | None = None) -> dict[str, Any]:
    params = params or StrategyParams()
    if len(bars) < max(params.ema_fast, params.ema_slow, params.atr_period) + 5:
        return {"symbol": symbol, "signal": "WAIT", "reason": "not_enough_data"}

    closes = [float(bar[4]) for bar in bars]
    lows = [float(bar[3]) for bar in bars]
    e_fast = ema(closes, params.ema_fast)
    e_slow = ema(closes, params.ema_slow)
    atr_values = atr(bars, params.atr_period)
    i = len(bars) - 1
    close = closes[i]
    trend = close > e_fast[i] > e_slow[i]
    pullback_recovered = lows[i] <= e_fast[i] and close > e_fast[i]
    volatility_ok = (atr_values[i] / close) >= params.min_atr_pct

    if trend and pullback_recovered and volatility_ok:
        stop = close - atr_values[i] * params.stop_atr
        take_profit = close + atr_values[i] * params.take_profit_atr
        return {
            "symbol": symbol,
            "signal": "LONG",
            "reason": "trend_pullback_recovered",
            "last_price": close,
            "ema_fast": e_fast[i],
            "ema_slow": e_slow[i],
            "atr": atr_values[i],
            "stop": stop,
            "take_profit": take_profit,
            "risk_pct": (close - stop) / close,
        }

    return {
        "symbol": symbol,
        "signal": "WAIT",
        "reason": "filters_not_aligned",
        "last_price": close,
        "ema_fast": e_fast[i],
        "ema_slow": e_slow[i],
        "atr": atr_values[i],
        "trend": trend,
        "pullback_recovered": pullback_recovered,
        "volatility_ok": volatility_ok,
    }


def backtest(symbol: str, bars: list[list[Any]], params: StrategyParams | None = None) -> dict[str, Any]:
    params = params or StrategyParams()
    if len(bars) < 80:
        return {"symbol": symbol, "trades": [], "summary": {"trades": 0, "win_rate": 0}}

    opens = [float(bar[1]) for bar in bars]
    highs = [float(bar[2]) for bar in bars]
    lows = [float(bar[3]) for bar in bars]
    closes = [float(bar[4]) for bar in bars]
    e_fast = ema(closes, params.ema_fast)
    e_slow = ema(closes, params.ema_slow)
    atr_values = atr(bars, params.atr_period)

    trades: list[dict[str, Any]] = []
    i = max(params.ema_slow, params.atr_period) + 10
    while i < len(bars) - 2:
        trend = closes[i] > e_fast[i] > e_slow[i]
        pullback_recovered = lows[i] <= e_fast[i] and closes[i] > e_fast[i]
        volatility_ok = (atr_values[i] / closes[i]) >= params.min_atr_pct
        if not (trend and pullback_recovered and volatility_ok):
            i += 1
            continue

        entry_index = i + 1
        entry = opens[entry_index]
        stop = entry - atr_values[i] * params.stop_atr
        take_profit = entry + atr_values[i] * params.take_profit_atr
        exit_price: float | None = None
        exit_reason = "timeout"
        exit_index = min(entry_index + params.max_hold_bars, len(bars) - 1)

        for j in range(entry_index, min(entry_index + params.max_hold_bars + 1, len(bars))):
            if lows[j] <= stop and highs[j] >= take_profit:
                exit_price = stop
                exit_reason = "stop_same_bar"
                exit_index = j
                break
            if lows[j] <= stop:
                exit_price = stop
                exit_reason = "stop"
                exit_index = j
                break
            if highs[j] >= take_profit:
                exit_price = take_profit
                exit_reason = "take_profit"
                exit_index = j
                break

        if exit_price is None:
            exit_price = closes[exit_index]

        gross_return = (exit_price - entry) / entry
        net_return = gross_return - params.taker_fee * 2
        trades.append(
            {
                "entry_time": bars[entry_index][0],
                "exit_time": bars[exit_index][0],
                "entry": entry,
                "exit": exit_price,
                "stop": stop,
                "take_profit": take_profit,
                "net_return_pct": net_return * 100,
                "exit_reason": exit_reason,
            }
        )
        i = exit_index + 1

    wins = [trade for trade in trades if trade["net_return_pct"] > 0]
    gains = sum(trade["net_return_pct"] for trade in trades if trade["net_return_pct"] > 0)
    losses = abs(sum(trade["net_return_pct"] for trade in trades if trade["net_return_pct"] < 0))
    total_return = sum(trade["net_return_pct"] for trade in trades)
    summary = {
        "symbol": symbol,
        "bars": len(bars),
        "trades": len(trades),
        "wins": len(wins),
        "win_rate": (len(wins) / len(trades) * 100) if trades else 0,
        "net_return_unlevered_pct": total_return,
        "avg_trade_pct": (total_return / len(trades)) if trades else 0,
        "profit_factor": (gains / losses) if losses else None,
    }
    return {"symbol": symbol, "summary": summary, "trades": trades[-100:]}
