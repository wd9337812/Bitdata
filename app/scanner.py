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
    limit = int(config.get("max_observation_symbols", config.get("max_scan_symbols", 30)))
    return merged[:limit]


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
        trigger_price = e10[i]
    elif strategy == "breakout":
        trend = close < e20[i] < e50[i] if is_short else close > e20[i] > e50[i]
        trigger_price = min(lows[max(0, i - 12):i]) if is_short else max(highs[max(0, i - 12):i])
        trigger = close < trigger_price if is_short else close > trigger_price
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
        trigger_price = e20[i]

    volatility_ok = (atr_value / close) >= min_atr
    distance_to_trigger_pct = (
        max(0.0, (close - trigger_price) / close * 100)
        if is_short
        else max(0.0, (trigger_price - close) / close * 100)
    )
    candle_move_pct = abs(close - closes[i - 1]) / close * 100 if i > 0 and close else 0.0
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
            "entry_type": "standard",
            "entry_type_label": "标准信号",
            "trigger_price": trigger_price,
            "distance_to_trigger_pct": 0.0,
            "candle_move_pct": candle_move_pct,
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
        "trigger_price": trigger_price,
        "distance_to_trigger_pct": distance_to_trigger_pct,
        "candle_move_pct": candle_move_pct,
        "entry_type": "watch",
        "entry_type_label": "观察",
    }


def _promote_wait_signal(
    signal: dict[str, Any],
    direction: str,
    entry_type: str,
    risk_multiplier: float,
) -> dict[str, Any]:
    close = float(signal["last_price"])
    atr_value = float(signal["atr"])
    is_short = direction == "SHORT"
    stop_mult = 1.05 if entry_type == "momentum" else 1.15
    take_mult = 2.15 if entry_type == "momentum" else 2.35
    stop = close + atr_value * stop_mult if is_short else close - atr_value * stop_mult
    take_profit = close - atr_value * take_mult if is_short else close + atr_value * take_mult
    return {
        **signal,
        "signal": direction,
        "reason": f"{direction.lower()}_{entry_type}",
        "stop": stop,
        "take_profit": take_profit,
        "risk_pct": abs(close - stop) / close,
        "expected_profit_pct": abs(take_profit - close) / close * 100,
        "entry_type": entry_type,
        "entry_type_label": "强动量" if entry_type == "momentum" else "抢跑试探",
        "risk_multiplier": risk_multiplier,
    }


def _current_signal_score(signal: dict[str, Any], direction: str, cost_ratio: float, recent: dict[str, Any]) -> float:
    score = 0.0
    if signal.get("signal") == direction:
        score += 55
    else:
        score += 15 if signal.get("trend") else 0
        score += 15 if signal.get("volatility_ok") else 0
        distance = float(signal.get("distance_to_trigger_pct") or 999)
        score += max(0.0, 25 - distance * 35)
        score += min(float(signal.get("candle_move_pct") or 0) * 7, 10)
    score += min(max(cost_ratio, 0), 8) * 2.5
    score += min(float(recent.get("profit_factor", 0)), 5) * 3
    return score


def _volume_spike_ratio(bars: list[list[Any]], lookback: int = 20) -> float:
    if len(bars) < lookback + 1:
        return 1.0
    current = float(bars[-1][5] or 0) * float(bars[-1][4] or 0)
    history = [
        float(bar[5] or 0) * float(bar[4] or 0)
        for bar in bars[-lookback - 1:-1]
    ]
    average = sum(history) / len(history) if history else 0
    return current / average if average > 0 else 1.0


def _depth_metrics(client: BinanceFuturesClient, symbol: str) -> dict[str, float | bool | str]:
    try:
        depth = client.depth(symbol, limit=5)
        bids = depth.get("bids", [])
        asks = depth.get("asks", [])
        if not bids or not asks:
            return {"available": False, "spread_pct": 999.0, "depth_notional": 0.0, "reason": "no_depth"}
        bid = float(bids[0][0])
        ask = float(asks[0][0])
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid * 100 if mid else 999.0
        bid_notional = sum(float(price) * float(qty) for price, qty in bids[:5])
        ask_notional = sum(float(price) * float(qty) for price, qty in asks[:5])
        return {
            "available": True,
            "spread_pct": spread_pct,
            "depth_notional": min(bid_notional, ask_notional),
        }
    except Exception as exc:
        return {"available": False, "spread_pct": 999.0, "depth_notional": 0.0, "reason": str(exc)}


def _score_volume(quote_volume: float) -> float:
    if quote_volume <= 30_000_000:
        return max(0.0, quote_volume / 30_000_000 * 8)
    if quote_volume >= 300_000_000:
        return 15.0
    return 8.0 + (quote_volume - 30_000_000) / 270_000_000 * 7.0


def _score_volatility(atr_pct: float) -> float:
    if atr_pct <= 0:
        return 0.0
    if 0.6 <= atr_pct <= 4.5:
        return 15.0
    if atr_pct < 0.6:
        return max(0.0, atr_pct / 0.6 * 15.0)
    return max(0.0, 15.0 - (atr_pct - 4.5) * 3.0)


def _score_spread_depth(depth: dict[str, Any], config: dict[str, Any]) -> float:
    max_spread = float(config.get("max_spread_pct", 0.08))
    min_depth = float(config.get("min_depth_notional_usdt", 20_000))
    spread = float(depth.get("spread_pct", 999))
    depth_notional = float(depth.get("depth_notional", 0))
    spread_score = max(0.0, 8.0 * (1.0 - spread / max(max_spread * 2, 0.0001)))
    depth_score = min(depth_notional / max(min_depth, 1), 1.0) * 7.0
    return spread_score + depth_score


def _simulation_quality(backtests: dict[int, dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    primary = backtests.get(5) or next(iter(backtests.values()), {})
    trades = int(primary.get("trades", 0))
    win_rate = float(primary.get("win_rate", 0))
    profit_factor = float(primary.get("profit_factor", 0))
    net_pct = float(primary.get("net_pct", 0))
    passed = (
        trades >= int(config.get("min_simulated_trades", 5))
        and win_rate >= float(config.get("min_simulated_win_rate", 45.0))
        and profit_factor >= float(config.get("min_simulated_profit_factor", 1.25))
        and net_pct >= float(config.get("min_simulated_net_pct", 1.5))
    )
    ten_day = backtests.get(10)
    if ten_day and float(ten_day.get("net_pct", 0)) < -2.0:
        passed = False
    return {
        "passed": passed,
        "trades": trades,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "net_pct": net_pct,
        "windows": backtests,
    }


def score_symbol_quality(
    symbol: str,
    bars: list[list[Any]],
    ticker: dict[str, Any],
    signal: dict[str, Any],
    backtests: dict[int, dict[str, Any]],
    depth: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    quote_volume = float(ticker.get("quoteVolume", 0))
    volume_score = _score_volume(quote_volume)
    spike = _volume_spike_ratio(bars)
    spike_score = min(spike / max(float(config.get("volume_spike_ratio", 1.8)), 0.1), 1.0) * 15.0
    price = float(signal.get("last_price") or ticker.get("lastPrice") or bars[-1][4] or 0)
    atr_pct = float(signal.get("atr") or 0) / price * 100 if price else 0.0
    volatility_score = _score_volatility(atr_pct)
    spread_depth_score = _score_spread_depth(depth, config)
    simulation = _simulation_quality(backtests, config)
    bt3 = backtests.get(3, {})
    bt5 = backtests.get(5, {})
    bt10 = backtests.get(10, {})
    bt3_score = 15.0 if float(bt3.get("profit_factor", 0)) >= 1.25 and float(bt3.get("net_pct", 0)) >= 2 else max(0.0, float(bt3.get("net_pct", 0)) * 1.5)
    bt5_score = 10.0 if float(bt5.get("profit_factor", 0)) >= 1.15 and int(bt5.get("trades", 0)) >= 8 else max(0.0, float(bt5.get("net_pct", 0)) * 0.6)
    bt10_score = 5.0 if not bt10 or float(bt10.get("net_pct", 0)) >= 0 else 0.0
    trend_score = 5.0 if signal.get("trend") is True or signal.get("signal") in {"LONG", "SHORT"} else 0.0
    false_breakout_penalty = 0.0
    primary = backtests.get(5) or {}
    if int(primary.get("trades", 0)) < int(config.get("min_simulated_trades", 5)):
        false_breakout_penalty += 8.0
    if float(primary.get("win_rate", 0)) < 35 and int(primary.get("trades", 0)) >= 5:
        false_breakout_penalty += 8.0
    if atr_pct > 5.5:
        false_breakout_penalty += 6.0
    score = (
        volume_score
        + spike_score
        + volatility_score
        + spread_depth_score
        + min(bt3_score, 15.0)
        + min(bt5_score, 10.0)
        + bt10_score
        + trend_score
        - min(false_breakout_penalty, 20.0)
    )
    score = max(0.0, min(score, 100.0))
    trade_score = float(config.get("symbol_trade_score", 75.0))
    small_score = float(config.get("symbol_small_trade_score", 65.0))
    observe_score = float(config.get("symbol_observe_score", 50.0))
    market_passed = (
        float(depth.get("spread_pct", 999)) <= float(config.get("max_spread_pct", 0.08))
        and float(depth.get("depth_notional", 0)) >= float(config.get("min_depth_notional_usdt", 20_000))
    )
    if score >= trade_score and simulation["passed"] and market_passed:
        pool = "trade"
        allowed = True
    elif score >= small_score and simulation["passed"] and market_passed:
        pool = "small_trade"
        allowed = True
    elif score >= observe_score:
        pool = "observe"
        allowed = False
    else:
        pool = "disabled"
        allowed = False
    return {
        "score": round(score, 2),
        "pool": pool,
        "allowed": allowed,
        "components": {
            "volume": round(volume_score, 2),
            "volume_spike": round(spike_score, 2),
            "volatility": round(volatility_score, 2),
            "spread_depth": round(spread_depth_score, 2),
            "backtest_3d": round(min(bt3_score, 15.0), 2),
            "backtest_5d": round(min(bt5_score, 10.0), 2),
            "backtest_10d": round(bt10_score, 2),
            "trend": round(trend_score, 2),
            "false_breakout_penalty": round(false_breakout_penalty, 2),
        },
        "market_passed": market_passed,
        "simulation": simulation,
        "market": {
            "quote_volume": quote_volume,
            "volume_spike_ratio": round(spike, 3),
            "atr_pct": round(atr_pct, 3),
            "spread_pct": round(float(depth.get("spread_pct", 999)), 4),
            "depth_notional": round(float(depth.get("depth_notional", 0)), 2),
        },
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
                history_passed = (
                    recent["trades"] >= min_trades
                    and recent["profit_factor"] >= min_pf
                    and recent["net_pct"] > min_net_pct
                )
                standard_passed = (
                    signal.get("signal") == direction
                    and history_passed
                    and expected_profit_pct >= float(config.get("min_expected_profit_pct", 0.35))
                    and cost_ratio >= float(config.get("min_expected_profit_cost_ratio", 3.0))
                )
                current_score = _current_signal_score(signal, direction, cost_ratio, recent)
                score = 0.0
                score += min(float(tickers.get(symbol, {}).get("quoteVolume", 0)) / 1_000_000_000, 5) * 0.5
                score += recent["net_pct"] * 0.15
                score += min(recent["profit_factor"], 10) * 2
                score += recent["win_rate"] * 0.05
                score += current_score
                score += 3 if standard_passed else 0
                score -= 1.5 if direction == "SHORT" else 0
                entry_type = "standard" if standard_passed else "watch"
                passed = standard_passed and score >= float(config.get("standard_min_score", 85.0))
                decision_reason = "标准突破信号通过" if passed else "等待触发"
                preemptive_enabled = mode["mode"] == "tournament" and config.get("preemptive_entries_enabled", True)
                if not passed and preemptive_enabled and history_passed and signal.get("signal") == "WAIT":
                    distance_pct = float(signal.get("distance_to_trigger_pct") or 999)
                    near_trigger = (
                        signal.get("trend") is True
                        and signal.get("volatility_ok") is True
                        and distance_pct <= float(config.get("preemptive_max_distance_pct", 0.35))
                    )
                    strong_momentum = (
                        signal.get("trend") is True
                        and signal.get("volatility_ok") is True
                        and float(signal.get("candle_move_pct") or 0) >= max(0.12, distance_pct)
                    )
                    min_preempt_score = float(config.get("preemptive_min_score", 72.0))
                    if score >= min_preempt_score and (near_trigger or strong_momentum):
                        entry_type = "momentum" if strong_momentum and not near_trigger else "preemptive"
                        risk_multiplier = (
                            float(config.get("short_preemptive_risk_multiplier", 0.33))
                            if direction == "SHORT"
                            else float(config.get("preemptive_risk_multiplier", 0.47))
                        )
                        signal = _promote_wait_signal(signal, direction, entry_type, risk_multiplier)
                        expected_profit_pct = float(signal.get("expected_profit_pct") or 0)
                        cost_ratio = expected_profit_pct / cost_pct if cost_pct else 0
                        risk_pct *= risk_multiplier
                        passed = (
                            expected_profit_pct >= float(config.get("min_expected_profit_pct", 0.35))
                            and cost_ratio >= max(1.5, float(config.get("min_expected_profit_cost_ratio", 3.0)) * 0.65)
                        )
                        decision_reason = "高分候选接近触发，允许小仓抢跑" if entry_type == "preemptive" else "短线强动量，允许小仓试探"
                    else:
                        misses = []
                        if not history_passed:
                            misses.append("历史回测不足")
                        if not signal.get("trend"):
                            misses.append("趋势未成立")
                        if not signal.get("volatility_ok"):
                            misses.append("波动不足")
                        if distance_pct > float(config.get("preemptive_max_distance_pct", 0.35)):
                            misses.append("距离触发价偏远")
                        if score < min_preempt_score:
                            misses.append("综合评分不足")
                        decision_reason = "、".join(misses) or "等待触发"
                candidates.append(
                    {
                        "symbol": symbol,
                        "direction": direction,
                        "mode": mode["mode"],
                        "strategy": mode["strategy"],
                        "score": round(score, 4),
                        "passed": passed,
                        "reason": "passed" if passed else "filters_not_passed",
                        "decision_reason": decision_reason,
                        "entry_type": entry_type,
                        "entry_type_label": signal.get("entry_type_label", "标准信号" if entry_type == "standard" else "观察"),
                        "current_score": round(current_score, 2),
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
    quality_days = sorted({
        int(day)
        for day in config.get("quality_backtest_days", [3, 5, 10])
        if int(day) > 0
    } | {int(mode["recent_days"])})

    for symbol in symbols:
        try:
            bars = client.klines_history(symbol, mode["interval"], max(quality_days))
            depth = _depth_metrics(client, symbol)
            directions = ["LONG", "SHORT"] if config.get("allow_short", False) else ["LONG"]
            for direction in directions:
                signal = latest_strategy_signal(symbol, bars, mode["strategy"], direction=direction)
                backtests = {
                    day: backtest_strategy(symbol, bars, mode["strategy"], day, direction=direction)
                    for day in quality_days
                }
                recent = backtests[int(mode["recent_days"])]
                ticker = tickers.get(symbol, {})
                last_price = float(signal.get("last_price") or ticker.get("lastPrice", 0))
                expected_profit_pct = float(signal.get("expected_profit_pct") or 0)
                cost_ratio = expected_profit_pct / cost_pct if cost_pct else 0
                quality = score_symbol_quality(symbol, bars, ticker, signal, backtests, depth, config)

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

                history_passed = (
                    recent["trades"] >= min_trades
                    and recent["profit_factor"] >= min_pf
                    and recent["net_pct"] > min_net_pct
                )
                standard_passed = (
                    signal.get("signal") == direction
                    and history_passed
                    and quality["allowed"]
                    and expected_profit_pct >= float(config.get("min_expected_profit_pct", 0.35))
                    and cost_ratio >= float(config.get("min_expected_profit_cost_ratio", 3.0))
                )
                current_score = _current_signal_score(signal, direction, cost_ratio, recent)
                score = 0.0
                score += min(float(ticker.get("quoteVolume", 0)) / 1_000_000_000, 5) * 0.5
                score += recent["net_pct"] * 0.15
                score += min(recent["profit_factor"], 10) * 2
                score += recent["win_rate"] * 0.05
                score += current_score
                score += quality["score"] * 0.25
                score += 3 if standard_passed else 0
                score -= 1.5 if direction == "SHORT" else 0

                entry_type = "standard" if standard_passed else "watch"
                passed = standard_passed and score >= float(config.get("standard_min_score", 85.0))
                decision_reason = "标准突破信号通过" if passed else "等待触发"
                if passed and quality["pool"] == "small_trade":
                    entry_type = "small_standard"
                    risk_pct *= float(config.get("small_trade_risk_multiplier", 0.5))
                    decision_reason = "币种质量允许小仓试探"
                if signal.get("signal") == direction and not quality["allowed"]:
                    decision_reason = f"币种质量未达实盘准入：{quality['pool']}，评分 {quality['score']}"

                preemptive_enabled = mode["mode"] == "tournament" and config.get("preemptive_entries_enabled", True)
                if not passed and preemptive_enabled and history_passed and quality["allowed"] and signal.get("signal") == "WAIT":
                    distance_pct = float(signal.get("distance_to_trigger_pct") or 999)
                    near_trigger = (
                        signal.get("trend") is True
                        and signal.get("volatility_ok") is True
                        and distance_pct <= float(config.get("preemptive_max_distance_pct", 0.35))
                    )
                    strong_momentum = (
                        signal.get("trend") is True
                        and signal.get("volatility_ok") is True
                        and float(signal.get("candle_move_pct") or 0) >= max(0.12, distance_pct)
                    )
                    min_preempt_score = float(config.get("preemptive_min_score", 72.0))
                    if score >= min_preempt_score and (near_trigger or strong_momentum):
                        entry_type = "momentum" if strong_momentum and not near_trigger else "preemptive"
                        risk_multiplier = (
                            float(config.get("short_preemptive_risk_multiplier", 0.33))
                            if direction == "SHORT"
                            else float(config.get("preemptive_risk_multiplier", 0.47))
                        )
                        if quality["pool"] == "small_trade":
                            risk_multiplier *= float(config.get("small_trade_risk_multiplier", 0.5))
                        signal = _promote_wait_signal(signal, direction, entry_type, risk_multiplier)
                        expected_profit_pct = float(signal.get("expected_profit_pct") or 0)
                        cost_ratio = expected_profit_pct / cost_pct if cost_pct else 0
                        risk_pct *= risk_multiplier
                        passed = (
                            expected_profit_pct >= float(config.get("min_expected_profit_pct", 0.35))
                            and cost_ratio >= max(1.5, float(config.get("min_expected_profit_cost_ratio", 3.0)) * 0.65)
                        )
                        decision_reason = "高分候选接近触发，允许小仓抢跑" if entry_type == "preemptive" else "短线强动量，允许小仓试探"
                    else:
                        misses = []
                        if not history_passed:
                            misses.append("历史回测不足")
                        if not quality["allowed"]:
                            misses.append(f"币种质量未达实盘准入({quality['pool']} {quality['score']})")
                        if not signal.get("trend"):
                            misses.append("趋势未成立")
                        if not signal.get("volatility_ok"):
                            misses.append("波动不足")
                        if distance_pct > float(config.get("preemptive_max_distance_pct", 0.35)):
                            misses.append("距离触发价偏远")
                        if score < min_preempt_score:
                            misses.append("综合评分不足")
                        decision_reason = "、".join(misses) or "等待触发"

                candidates.append(
                    {
                        "symbol": symbol,
                        "direction": direction,
                        "mode": mode["mode"],
                        "strategy": mode["strategy"],
                        "score": round(score, 4),
                        "passed": passed,
                        "reason": "passed" if passed else "filters_not_passed",
                        "decision_reason": decision_reason,
                        "entry_type": entry_type,
                        "entry_type_label": signal.get("entry_type_label", "标准信号" if entry_type == "standard" else "观察"),
                        "symbol_quality": quality,
                        "symbol_pool": quality["pool"],
                        "simulation_passed": quality["simulation"]["passed"],
                        "current_score": round(current_score, 2),
                        "signal": signal,
                        "recent": recent,
                        "backtests": backtests,
                        "ticker": {
                            "last": last_price,
                            "change_pct": float(ticker.get("priceChangePercent", 0)),
                            "volume_usdt_b": round(float(ticker.get("quoteVolume", 0)) / 1_000_000_000, 3),
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

    candidates.sort(
        key=lambda item: (
            item.get("passed", False),
            item.get("symbol_quality", {}).get("allowed", False),
            item.get("symbol_quality", {}).get("score", -999),
            item.get("score", -999),
        ),
        reverse=True,
    )
    candidates = [_json_safe(candidate) for candidate in candidates]
    max_candidates = int(config.get("max_scan_symbols", 30))
    trade_pool = [
        candidate
        for candidate in candidates
        if candidate.get("symbol_quality", {}).get("pool") in {"trade", "small_trade"}
    ][: int(config.get("max_trade_pool_symbols", 15))]
    observe_pool = [
        candidate
        for candidate in candidates
        if candidate.get("symbol_quality", {}).get("pool") == "observe"
    ][:max_candidates]
    return {
        "mode": _json_safe(mode),
        "symbols": symbols,
        "trade_pool": trade_pool,
        "observe_pool": observe_pool,
        "candidates": candidates[:max_candidates],
        "best": candidates[0] if candidates else None,
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return 999.0 if value > 0 else 0.0
    return value
