from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from typing import Any

from app.binance_client import BinanceFuturesClient
from app.strategy import StrategyParams, atr, ema


MODE_PRESETS: dict[str, dict[str, Any]] = {
    "conservative": {
        "strategy": "default",
        "interval_key": "conservative_interval",
        "recent_days_key": "conservative_recent_days",
        "risk_key": "risk_per_trade_pct",
        "leverage_key": "stage1_max_leverage",
        "margin_key": "max_symbol_margin_pct",
        "min_pf": 1.2,
        "min_trades": 2,
        "recent_days": 30,
    },
    "balanced": {
        "strategy": "default",
        "interval_key": "balanced_interval",
        "recent_days_key": "balanced_recent_days",
        "risk_key": "risk_per_trade_pct",
        "leverage_key": "stage1_max_leverage",
        "margin_key": "max_symbol_margin_pct",
        "min_pf": 1.05,
        "min_trades": 2,
        "recent_days": 20,
    },
    "attack": {
        "strategy": "attack",
        "interval_key": "attack_interval",
        "recent_days_key": "attack_recent_days",
        "risk_key": "attack_risk_per_trade_pct",
        "leverage_key": "attack_max_leverage",
        "margin_key": "attack_max_symbol_margin_pct",
        "min_pf": 1.1,
        "min_trades": 3,
        "recent_days": 10,
    },
    "tournament": {
        "strategy": "breakout",
        "interval_key": "tournament_interval",
        "recent_days_key": "tournament_recent_days",
        "risk_key": "tournament_risk_per_trade_pct",
        "leverage_key": "tournament_max_leverage",
        "margin_key": "tournament_max_symbol_margin_pct",
        "min_pf": 1.0,
        "min_trades": 1,
        "recent_days": 5,
    },
}


def active_growth_mode(config: dict[str, Any], equity: float | None = None) -> str:
    configured = str(config.get("growth_mode", "balanced")).lower()
    if not config.get("auto_risk_by_equity", True):
        return configured if configured in MODE_PRESETS else "balanced"
    if equity is None:
        return configured if configured in MODE_PRESETS else "balanced"
    if equity < 100:
        return "tournament"
    if equity < 500:
        return "attack"
    return configured if configured in MODE_PRESETS else "balanced"


def mode_config(config: dict[str, Any], equity: float | None = None) -> dict[str, Any]:
    mode = active_growth_mode(config, equity)
    preset = MODE_PRESETS[mode].copy()
    preset["mode"] = mode
    preset["risk_pct"] = float(config.get(preset["risk_key"], config.get("risk_per_trade_pct", 1.0)))
    preset["leverage"] = float(config.get(preset["leverage_key"], config.get("stage1_max_leverage", 2)))
    preset["margin_pct"] = float(config.get(preset["margin_key"], config.get("max_symbol_margin_pct", 35.0)))
    preset["interval"] = str(config.get(preset["interval_key"], config.get("interval", "4h")))
    preset["recent_days"] = int(config.get(preset["recent_days_key"], preset["recent_days"]))
    preset["min_pf"] = float(config.get("min_profit_factor", preset["min_pf"])) if mode in {"conservative", "balanced"} else preset["min_pf"]
    preset["min_trades"] = int(config.get("min_recent_trades", preset["min_trades"])) if mode in {"conservative", "balanced"} else preset["min_trades"]
    return preset


def discover_coin_symbols(client: BinanceFuturesClient, config: dict[str, Any]) -> list[str]:
    if not config.get("auto_discover_symbols", True):
        return [symbol.upper() for symbol in config.get("stage1_symbols", ["SOLUSDT"])]

    exchange_info = client.exchange_info()
    coin_symbols = {
        item["symbol"]
        for item in exchange_info.get("symbols", [])
        if item.get("contractType") == "PERPETUAL"
        and item.get("underlyingType") == "COIN"
        and item.get("status") == "TRADING"
        and item.get("quoteAsset") == "USDT"
    }
    min_volume = float(config.get("min_24h_volume_usdt", 100_000_000))
    tickers = client.ticker_24h()
    ranked = [
        (item["symbol"], float(item.get("quoteVolume", 0)))
        for item in tickers
        if item["symbol"] in coin_symbols and float(item.get("quoteVolume", 0)) >= min_volume
    ]
    ranked.sort(key=lambda row: row[1], reverse=True)
    manual = [symbol.upper() for symbol in config.get("stage1_symbols", [])]
    merged = []
    for symbol in manual + [symbol for symbol, _ in ranked]:
        if symbol in coin_symbols and symbol not in merged:
            merged.append(symbol)
    return merged[: int(config.get("max_scan_symbols", 30))]


def latest_strategy_signal(
    symbol: str,
    bars: list[list[Any]],
    strategy: str,
    params: StrategyParams | None = None,
    direction: str = "LONG",
) -> dict[str, Any]:
    params = params or StrategyParams()
    if len(bars) < 80:
        return {"symbol": symbol, "signal": "WAIT", "reason": "not_enough_data"}

    closes = [float(bar[4]) for bar in bars]
    highs = [float(bar[2]) for bar in bars]
    lows = [float(bar[3]) for bar in bars]
    e10 = ema(closes, 10)
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    atr_values = atr(bars, params.atr_period)
    i = len(bars) - 1
    close = closes[i]
    atr_value = atr_values[i]

    is_short = direction.upper() == "SHORT"
    if strategy == "attack":
        trend = close < e10[i] < e20[i] if is_short else close > e10[i] > e20[i]
        trigger = (
            (highs[i] >= e10[i] and close < e10[i]) or (close < lows[i - 1] and closes[i - 1] < e10[i - 1])
            if is_short
            else (lows[i] <= e10[i] and close > e10[i]) or (close > highs[i - 1] and closes[i - 1] > e10[i - 1])
        )
        stop_mult = 1.0
        take_mult = 2.2
        min_atr = 0.006
        reason = "short_attack_pullback_or_momentum" if is_short else "attack_pullback_or_momentum"
    elif strategy == "breakout":
        trend = close < e20[i] < e50[i] if is_short else close > e20[i] > e50[i]
        trigger = close < min(lows[max(0, i - 12):i]) if is_short else close > max(highs[max(0, i - 12):i])
        stop_mult = 1.2
        take_mult = 2.5
        min_atr = 0.006
        reason = "short_breakout" if is_short else "breakout"
    else:
        trend = close < e20[i] < e50[i] if is_short else close > e20[i] > e50[i]
        trigger = highs[i] >= e20[i] and close < e20[i] if is_short else lows[i] <= e20[i] and close > e20[i]
        stop_mult = params.stop_atr
        take_mult = params.take_profit_atr
        min_atr = params.min_atr_pct
        reason = "short_trend_pullback_recovered" if is_short else "trend_pullback_recovered"

    volatility_ok = (atr_value / close) >= min_atr
    if trend and trigger and volatility_ok:
        stop = close + atr_value * stop_mult if is_short else close - atr_value * stop_mult
        take_profit = close - atr_value * take_mult if is_short else close + atr_value * take_mult
        return {
            "symbol": symbol,
            "signal": "SHORT" if is_short else "LONG",
            "reason": reason,
            "strategy": strategy,
            "last_price": close,
            "ema_fast": e10[i] if strategy == "attack" else e20[i],
            "ema_slow": e20[i] if strategy == "attack" else e50[i],
            "atr": atr_value,
            "stop": stop,
            "take_profit": take_profit,
            "risk_pct": abs(close - stop) / close,
            "expected_profit_pct": abs(take_profit - close) / close * 100,
        }

    return {
        "symbol": symbol,
        "signal": "WAIT",
        "reason": "filters_not_aligned",
        "strategy": strategy,
        "last_price": close,
        "ema_fast": e10[i] if strategy == "attack" else e20[i],
        "ema_slow": e20[i] if strategy == "attack" else e50[i],
        "atr": atr_value,
        "direction": "SHORT" if is_short else "LONG",
        "trend": trend,
        "trigger": trigger,
        "volatility_ok": volatility_ok,
    }


def backtest_strategy(symbol: str, bars: list[list[Any]], strategy: str, days: int, direction: str = "LONG") -> dict[str, Any]:
    if len(bars) < 100:
        return {"symbol": symbol, "trades": 0, "wins": 0, "win_rate": 0, "net_pct": 0, "profit_factor": 0}

    params = StrategyParams()
    opens = [float(bar[1]) for bar in bars]
    highs = [float(bar[2]) for bar in bars]
    lows = [float(bar[3]) for bar in bars]
    closes = [float(bar[4]) for bar in bars]
    e10 = ema(closes, 10)
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    atr_values = atr(bars, params.atr_period)
    cutoff = datetime.fromtimestamp(bars[-1][0] / 1000, timezone.utc) - timedelta(days=days)
    is_short = direction.upper() == "SHORT"

    trades: list[dict[str, Any]] = []
    i = 60
    while i < len(bars) - 2:
        if strategy == "attack":
            trend = closes[i] < e10[i] < e20[i] if is_short else closes[i] > e10[i] > e20[i]
            trigger = (
                (highs[i] >= e10[i] and closes[i] < e10[i]) or (closes[i] < lows[i - 1] and closes[i - 1] < e10[i - 1])
                if is_short
                else (lows[i] <= e10[i] and closes[i] > e10[i]) or (closes[i] > highs[i - 1] and closes[i - 1] > e10[i - 1])
            )
            stop_mult = 1.0
            take_mult = 2.2
            max_hold = 12
            min_atr = 0.006
        elif strategy == "breakout":
            trend = closes[i] < e20[i] < e50[i] if is_short else closes[i] > e20[i] > e50[i]
            trigger = closes[i] < min(lows[max(0, i - 12):i]) if is_short else closes[i] > max(highs[max(0, i - 12):i])
            stop_mult = 1.2
            take_mult = 2.5
            max_hold = 10
            min_atr = 0.006
        else:
            trend = closes[i] < e20[i] < e50[i] if is_short else closes[i] > e20[i] > e50[i]
            trigger = highs[i] >= e20[i] and closes[i] < e20[i] if is_short else lows[i] <= e20[i] and closes[i] > e20[i]
            stop_mult = params.stop_atr
            take_mult = params.take_profit_atr
            max_hold = params.max_hold_bars
            min_atr = params.min_atr_pct

        if not (trend and trigger and (atr_values[i] / closes[i]) >= min_atr):
            i += 1
            continue

        entry_index = i + 1
        entry = opens[entry_index]
        stop = entry + atr_values[i] * stop_mult if is_short else entry - atr_values[i] * stop_mult
        take_profit = entry - atr_values[i] * take_mult if is_short else entry + atr_values[i] * take_mult
        exit_index = min(entry_index + max_hold, len(bars) - 1)
        exit_price = None
        exit_reason = "timeout"
        for j in range(entry_index, min(entry_index + max_hold + 1, len(bars))):
            if is_short and highs[j] >= stop and lows[j] <= take_profit:
                exit_price = stop
                exit_reason = "stop_same_bar"
                exit_index = j
                break
            if (not is_short) and lows[j] <= stop and highs[j] >= take_profit:
                exit_price = stop
                exit_reason = "stop_same_bar"
                exit_index = j
                break
            if is_short and highs[j] >= stop:
                exit_price = stop
                exit_reason = "stop"
                exit_index = j
                break
            if (not is_short) and lows[j] <= stop:
                exit_price = stop
                exit_reason = "stop"
                exit_index = j
                break
            if is_short and lows[j] <= take_profit:
                exit_price = take_profit
                exit_reason = "take_profit"
                exit_index = j
                break
            if (not is_short) and highs[j] >= take_profit:
                exit_price = take_profit
                exit_reason = "take_profit"
                exit_index = j
                break
        if exit_price is None:
            exit_price = closes[exit_index]

        entry_time = datetime.fromtimestamp(bars[entry_index][0] / 1000, timezone.utc)
        if entry_time >= cutoff:
            gross_return = (entry - exit_price) / entry if is_short else (exit_price - entry) / entry
            net_return = gross_return - params.taker_fee * 2
            trades.append({"entry_time": bars[entry_index][0], "net_return_pct": net_return * 100, "exit_reason": exit_reason})
        i = exit_index + 1

    wins = [trade for trade in trades if trade["net_return_pct"] > 0]
    gains = sum(trade["net_return_pct"] for trade in trades if trade["net_return_pct"] > 0)
    losses = abs(sum(trade["net_return_pct"] for trade in trades if trade["net_return_pct"] < 0))
    return {
        "symbol": symbol,
        "strategy": strategy,
        "direction": "SHORT" if is_short else "LONG",
        "days": days,
        "trades": len(trades),
        "wins": len(wins),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0,
        "net_pct": sum(trade["net_return_pct"] for trade in trades),
        "profit_factor": gains / losses if losses else (999 if gains > 0 else 0),
    }


def scan_growth_candidates(
    client: BinanceFuturesClient,
    config: dict[str, Any],
    account_summary: dict[str, Any],
) -> dict[str, Any]:
    equity = account_summary.get("equity")
    mode = mode_config(config, equity)
    symbols = discover_coin_symbols(client, config)
    candidates = []
    tickers = {item["symbol"]: item for item in client.ticker_24h(symbols)}
    fee_pct = StrategyParams().taker_fee * 2 * 100
    slippage_pct = float(config.get("estimated_slippage_pct", 0.04))
    cost_pct = fee_pct + slippage_pct

    for symbol in symbols:
        try:
            bars = client.klines_history(symbol, mode["interval"], int(mode["recent_days"]))
            directions = ["LONG", "SHORT"] if config.get("allow_short", False) else ["LONG"]
            for direction in directions:
                signal = latest_strategy_signal(symbol, bars, mode["strategy"], direction=direction)
                recent = backtest_strategy(symbol, bars, mode["strategy"], int(mode["recent_days"]), direction=direction)
                last_price = float(signal.get("last_price") or tickers.get(symbol, {}).get("lastPrice", 0))
                expected_profit_pct = float(signal.get("expected_profit_pct") or 0)
                cost_ratio = expected_profit_pct / cost_pct if cost_pct else 0
                if direction == "SHORT":
                    min_trades = int(config.get("short_min_recent_trades", 5))
                    min_pf = float(config.get("short_min_profit_factor", 1.3))
                    min_net_pct = float(config.get("short_min_net_pct", 1.0))
                    risk_pct = float(mode["risk_pct"]) * float(config.get("short_risk_multiplier", 0.5))
                else:
                    min_trades = int(mode["min_trades"])
                    min_pf = float(mode["min_pf"])
                    min_net_pct = 0.0
                    risk_pct = float(mode["risk_pct"])
                passed = (
                    signal.get("signal") == direction
                    and recent["trades"] >= min_trades
                    and recent["profit_factor"] >= min_pf
                    and recent["net_pct"] > min_net_pct
                    and expected_profit_pct >= float(config.get("min_expected_profit_pct", 0.35))
                    and cost_ratio >= float(config.get("min_expected_profit_cost_ratio", 3.0))
                )
                score = 0.0
                score += min(float(tickers.get(symbol, {}).get("quoteVolume", 0)) / 1_000_000_000, 5) * 0.5
                score += recent["net_pct"] * 0.15
                score += min(recent["profit_factor"], 10) * 2
                score += recent["win_rate"] * 0.05
                score += 10 if signal.get("signal") == direction else 0
                score += 3 if passed else 0
                score -= 1.5 if direction == "SHORT" else 0
                candidates.append(
                    {
                        "symbol": symbol,
                        "direction": direction,
                        "mode": mode["mode"],
                        "strategy": mode["strategy"],
                        "score": round(score, 4),
                        "passed": passed,
                        "reason": "passed" if passed else "filters_not_passed",
                        "signal": signal,
                        "recent": recent,
                        "ticker": {
                            "last": last_price,
                            "change_pct": float(tickers.get(symbol, {}).get("priceChangePercent", 0)),
                            "volume_usdt_b": round(float(tickers.get(symbol, {}).get("quoteVolume", 0)) / 1_000_000_000, 3),
                        },
                        "cost_ratio": cost_ratio,
                        "fee_pct": fee_pct,
                        "estimated_slippage_pct": slippage_pct,
                        "estimated_cost_pct": cost_pct,
                        "expected_profit_pct": expected_profit_pct,
                        "risk_pct": risk_pct,
                        "base_risk_pct": mode["risk_pct"],
                        "leverage": mode["leverage"],
                        "margin_pct": mode["margin_pct"],
                        "thresholds": {"min_trades": min_trades, "min_pf": min_pf, "min_net_pct": min_net_pct},
                    }
                )
        except Exception as exc:
            candidates.append({"symbol": symbol, "passed": False, "reason": str(exc), "score": -999})

    candidates.sort(key=lambda item: (item.get("passed", False), item.get("score", -999)), reverse=True)
    candidates = [_json_safe(candidate) for candidate in candidates]
    return {"mode": _json_safe(mode), "symbols": symbols, "candidates": candidates[:30], "best": candidates[0] if candidates else None}


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return 999.0 if value > 0 else 0.0
    return value
