from __future__ import annotations

from typing import Any

from app.strategy import atr, ema


def _stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    wins = [trade for trade in trades if float(trade["net_return_pct"]) > 0]
    gains = sum(float(trade["net_return_pct"]) for trade in wins)
    losses = abs(sum(float(trade["net_return_pct"]) for trade in trades if float(trade["net_return_pct"]) < 0))
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for trade in trades:
        equity *= 1 + float(trade["net_return_pct"]) / 100
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak * 100)
    return {
        "trades": len(trades),
        "wins": len(wins),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0.0,
        "net_pct": (equity - 1) * 100,
        "profit_factor": gains / losses if losses else (999.0 if gains > 0 else 0.0),
        "max_drawdown_pct": max_drawdown,
    }


def backtest_v31_bars(
    bars: list[list[Any]],
    *,
    cost_pct: float = 0.12,
    path_filter: bool = True,
) -> dict[str, Any]:
    """No-lookahead 1h trend validation used only by the manual offline endpoint."""
    if len(bars) < 260:
        return {"status": "not_enough_data", "trades": 0}
    closes = [float(row[4]) for row in bars]
    highs = [float(row[2]) for row in bars]
    lows = [float(row[3]) for row in bars]
    opens = [float(row[1]) for row in bars]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    atr_values = atr(bars, 20)
    trades: list[dict[str, Any]] = []
    index = 200
    while index < len(bars) - 26:
        path = closes[index - 72 : index + 1]
        travelled = sum(abs(path[pos] - path[pos - 1]) for pos in range(1, len(path)))
        efficiency = abs(path[-1] - path[0]) / travelled if travelled > 0 else 0.0
        upper = max(highs[index - 24 : index])
        lower = min(lows[index - 24 : index])
        long_signal = closes[index] > upper and closes[index] > e20[index] > e50[index]
        short_signal = closes[index] < lower and closes[index] < e20[index] < e50[index]
        if path_filter and efficiency < 0.18:
            index += 1
            continue
        if not (long_signal or short_signal):
            index += 1
            continue
        direction = "LONG" if long_signal else "SHORT"
        entry_index = index + 1
        entry = opens[entry_index]
        atr_value = float(atr_values[index] or 0)
        if entry <= 0 or atr_value <= 0:
            index += 1
            continue
        stop = entry - atr_value if direction == "LONG" else entry + atr_value
        take = entry + atr_value * 2.8 if direction == "LONG" else entry - atr_value * 2.8
        trail_active = False
        exit_price = closes[min(entry_index + 24, len(bars) - 1)]
        exit_index = min(entry_index + 24, len(bars) - 1)
        reason = "time_exit"
        for pos in range(entry_index, exit_index + 1):
            favorable = highs[pos] - entry if direction == "LONG" else entry - lows[pos]
            if favorable >= atr_value * 1.2:
                trail_active = True
                candidate = closes[pos] - atr_value * 0.7 if direction == "LONG" else closes[pos] + atr_value * 0.7
                stop = max(stop, candidate) if direction == "LONG" else min(stop, candidate)
            stop_hit = lows[pos] <= stop if direction == "LONG" else highs[pos] >= stop
            take_hit = highs[pos] >= take if direction == "LONG" else lows[pos] <= take
            if stop_hit or take_hit:
                exit_price = stop if stop_hit else take
                exit_index = pos
                reason = "trailing_stop" if stop_hit and trail_active else "stop" if stop_hit else "take_profit"
                break
        gross = (exit_price - entry) / entry * 100
        if direction == "SHORT":
            gross = -gross
        trades.append(
            {
                "direction": direction,
                "entry_time": bars[entry_index][0],
                "exit_time": bars[exit_index][0],
                "net_return_pct": gross - cost_pct,
                "exit_reason": reason,
                "path_efficiency": efficiency,
            }
        )
        index = exit_index + 1
    split = max(1, int(len(trades) * 0.70))
    return {
        "status": "ok",
        "engine": "v31_medium_trend_validation",
        "cost_pct": cost_pct,
        "path_filter": path_filter,
        "all": _stats(trades),
        "in_sample": _stats(trades[:split]),
        "out_of_sample": _stats(trades[split:]),
        "recent_trades": trades[-20:],
    }


def compare_v31_to_plain_breakout(bars: list[list[Any]], cost_pct: float = 0.12) -> dict[str, Any]:
    return {
        "v31": backtest_v31_bars(bars, cost_pct=cost_pct, path_filter=True),
        "plain_donchian": backtest_v31_bars(bars, cost_pct=cost_pct, path_filter=False),
    }
