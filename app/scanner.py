from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import time
from typing import Any

from app.binance_client import BinanceFuturesClient
from app.live_learning import apply_live_credit_to_candidate
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
    "tournament_sprint": {
        "strategy": "breakout",
        "interval_key": "tournament_sprint_interval",
        "recent_days_key": "tournament_sprint_recent_days",
        "risk_key": "tournament_sprint_risk_per_trade_pct",
        "leverage_key": "tournament_sprint_max_leverage",
        "margin_key": "tournament_sprint_max_symbol_margin_pct",
        "min_pf": 0.85,
        "min_trades": 1,
        "recent_days": 3,
    },
}


PIPELINE_DEFAULTS: dict[str, dict[str, int]] = {
    "conservative": {"recall": 180, "coarse": 70, "rank": 45, "auction": 8},
    "balanced": {"recall": 250, "coarse": 100, "rank": 60, "auction": 10},
    "attack": {"recall": 350, "coarse": 140, "rank": 70, "auction": 12},
    "tournament": {"recall": 500, "coarse": 180, "rank": 80, "auction": 15},
    "tournament_sprint": {"recall": 600, "coarse": 220, "rank": 90, "auction": 15},
}


def active_growth_mode(config: dict[str, Any], equity: float | None = None) -> str:
    configured = str(config.get("growth_mode", "balanced")).lower()
    if not config.get("auto_risk_by_equity", True):
        return configured if configured in MODE_PRESETS else "balanced"
    if equity is None:
        return configured if configured in MODE_PRESETS else "balanced"
    if (
        config.get("tournament_sprint_enabled", True)
        and equity < float(config.get("tournament_sprint_auto_under_equity", 0))
    ):
        return "tournament_sprint"
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


def pipeline_limits(config: dict[str, Any], mode: dict[str, Any] | str) -> dict[str, int]:
    mode_name = str(mode.get("mode") if isinstance(mode, dict) else mode)
    defaults = PIPELINE_DEFAULTS.get(mode_name, PIPELINE_DEFAULTS["balanced"])
    return {
        "recall": int(config.get("recall_pool_limit", config.get("max_observation_symbols", defaults["recall"]))),
        "coarse": int(config.get("coarse_pool_limit", defaults["coarse"])),
        "rank": int(config.get("rank_pool_limit", defaults["rank"])),
        "auction": int(config.get("auction_pool_limit", config.get("depth_check_top_symbols", defaults["auction"]))),
    }


def strategy_params_for_mode(
    config: dict[str, Any],
    mode: dict[str, Any] | str,
    entry_type: str = "standard",
) -> StrategyParams | None:
    mode_name = str(mode.get("mode") if isinstance(mode, dict) else mode)
    if mode_name != "tournament_sprint":
        return None
    entry_key = "momentum" if entry_type == "momentum" else "preemptive" if entry_type == "preemptive" else "standard"
    defaults = {
        "standard": (0.9, 1.4, 6),
        "preemptive": (0.75, 1.0, 4),
        "momentum": (0.8, 1.2, 5),
    }
    default_stop, default_take, default_hold = defaults[entry_key]
    return StrategyParams(
        stop_atr=float(config.get(f"tournament_sprint_{entry_key}_stop_atr", default_stop)),
        take_profit_atr=float(config.get(f"tournament_sprint_{entry_key}_take_profit_atr", default_take)),
        max_hold_bars=int(config.get(f"tournament_sprint_{entry_key}_max_hold_bars", default_hold)),
        min_atr_pct=0.006,
    )


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
    limit = int(config.get("recall_pool_limit", config.get("max_observation_symbols", config.get("max_scan_symbols", 30))))
    return merged[:limit]


def latest_strategy_signal(
    symbol: str,
    bars: list[list[Any]],
    strategy: str,
    params: StrategyParams | None = None,
    direction: str = "LONG",
) -> dict[str, Any]:
    custom_params = params is not None
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
        stop_mult = params.stop_atr if custom_params else 1.2
        take_mult = params.take_profit_atr if custom_params else 2.5
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
            "protection_profile": {
                "stop_atr": stop_mult,
                "take_profit_atr": take_mult,
                "max_hold_bars": params.max_hold_bars if custom_params else None,
            },
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
    params: StrategyParams | None = None,
) -> dict[str, Any]:
    close = float(signal["last_price"])
    atr_value = float(signal["atr"])
    is_short = direction == "SHORT"
    if params is not None:
        stop_mult = params.stop_atr
        take_mult = params.take_profit_atr
        max_hold_bars = params.max_hold_bars
    else:
        stop_mult = 1.05 if entry_type == "momentum" else 1.15
        take_mult = 2.15 if entry_type == "momentum" else 2.35
        max_hold_bars = None
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
        "protection_profile": {
            "stop_atr": stop_mult,
            "take_profit_atr": take_mult,
            "max_hold_bars": max_hold_bars,
        },
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


def _backtest_strategy_with_params(
    symbol: str,
    bars: list[list[Any]],
    strategy: str,
    days: int,
    direction: str,
    params: StrategyParams | None,
) -> dict[str, Any]:
    try:
        return backtest_strategy(symbol, bars, strategy, days, direction=direction, params=params)
    except TypeError as exc:
        if "params" not in str(exc):
            raise
        return backtest_strategy(symbol, bars, strategy, days, direction=direction)


def _coarse_rank_symbols(
    symbols: list[str],
    tickers: dict[str, dict[str, Any]],
    config: dict[str, Any],
    mode: dict[str, Any],
) -> tuple[list[str], list[dict[str, Any]]]:
    limits = pipeline_limits(config, mode)
    manual = {symbol.upper() for symbol in config.get("stage1_symbols", [])}
    min_volume = float(config.get("min_24h_volume_usdt", 0))
    rows: list[dict[str, Any]] = []
    for index, symbol in enumerate(symbols):
        ticker = tickers.get(symbol, {})
        quote_volume = float(ticker.get("quoteVolume", 0) or 0)
        change_pct = float(ticker.get("priceChangePercent", 0) or 0)
        last_price = float(ticker.get("lastPrice", 0) or 0)
        volume_score = min(max(math.log10(max(quote_volume, 1)) - 6.5, 0.0) * 12.0, 35.0)
        move_score = min(abs(change_pct) * 1.8, 28.0)
        direction_bias = 4.0 if change_pct > 0 else 2.0 if change_pct < 0 else 0.0
        manual_score = 30.0 if symbol in manual else 0.0
        liquidity_penalty = 18.0 if quote_volume < min_volume else 0.0
        new_tail_boost = max(0.0, 10.0 - index * 0.03)
        score = volume_score + move_score + direction_bias + manual_score + new_tail_boost - liquidity_penalty
        reasons = []
        if symbol in manual:
            reasons.append("manual")
        if quote_volume >= min_volume:
            reasons.append("liquid")
        if abs(change_pct) >= 8:
            reasons.append("large_move")
        if quote_volume < min_volume:
            reasons.append("low_volume")
        rows.append(
            {
                "symbol": symbol,
                "score": round(score, 4),
                "quote_volume": quote_volume,
                "change_pct": change_pct,
                "last_price": last_price,
                "reasons": reasons,
            }
        )
    rows.sort(key=lambda item: (item["symbol"] in manual, item["score"]), reverse=True)
    ranked = rows[: max(1, limits["coarse"])]
    symbols_out = [item["symbol"] for item in ranked[: max(1, limits["rank"])]]
    return symbols_out, ranked


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


def _unchecked_depth_metrics() -> dict[str, float | bool | str]:
    return {
        "available": False,
        "spread_pct": 999.0,
        "depth_notional": 0.0,
        "reason": "depth_not_checked",
    }


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


QUALITY_WEIGHTS: dict[str, dict[str, float]] = {
    "conservative": {
        "volume": 16,
        "volume_spike": 8,
        "volatility": 11,
        "spread_depth": 19,
        "backtest_3d": 9,
        "backtest_5d": 16,
        "backtest_10d": 13,
        "trend": 8,
    },
    "balanced": {
        "volume": 15,
        "volume_spike": 11,
        "volatility": 13,
        "spread_depth": 18,
        "backtest_3d": 11,
        "backtest_5d": 15,
        "backtest_10d": 9,
        "trend": 8,
    },
    "attack": {
        "volume": 12,
        "volume_spike": 14,
        "volatility": 15,
        "spread_depth": 17,
        "backtest_3d": 14,
        "backtest_5d": 13,
        "backtest_10d": 6,
        "trend": 9,
    },
    "tournament": {
        "volume": 10,
        "volume_spike": 16,
        "volatility": 17,
        "spread_depth": 16,
        "backtest_3d": 16,
        "backtest_5d": 11,
        "backtest_10d": 4,
        "trend": 10,
    },
    "tournament_sprint": {
        "volume": 8,
        "volume_spike": 20,
        "volatility": 19,
        "spread_depth": 18,
        "backtest_3d": 15,
        "backtest_5d": 8,
        "backtest_10d": 1,
        "trend": 11,
    },
}


ATR_IDEAL_RANGES: dict[str, tuple[float, float, float]] = {
    "conservative": (0.4, 2.5, 4.0),
    "balanced": (0.5, 3.5, 5.0),
    "attack": (0.8, 4.5, 7.0),
    "tournament": (1.0, 5.5, 8.0),
}


def _mode_name(config: dict[str, Any], mode: dict[str, Any] | None = None) -> str:
    name = str((mode or {}).get("mode") or config.get("growth_mode") or "balanced").lower()
    return name if name in QUALITY_WEIGHTS else "balanced"


def _score_volatility_for_mode(atr_pct: float, mode_name: str, config: dict[str, Any]) -> float:
    if atr_pct <= 0:
        return 0.0
    if mode_name == "tournament_sprint":
        ideal_min = float(config.get("sprint_atr_ideal_min_pct", 1.2))
        ideal_max = float(config.get("sprint_atr_ideal_max_pct", 7.0))
        high = float(config.get("sprint_atr_high_pct", 10.0))
    else:
        ideal_min, ideal_max, high = ATR_IDEAL_RANGES.get(mode_name, ATR_IDEAL_RANGES["balanced"])
    if ideal_min <= atr_pct <= ideal_max:
        return 1.0
    if atr_pct < ideal_min:
        return max(0.0, atr_pct / max(ideal_min, 0.0001))
    if atr_pct <= high:
        return max(0.45, 1.0 - (atr_pct - ideal_max) / max(high - ideal_max, 0.0001) * 0.4)
    return max(0.0, 0.6 - (atr_pct - high) * 0.2)


def _quality_risk_multiplier(
    *,
    mode_name: str,
    pool: str,
    atr_pct: float,
    sample_low: bool,
    sample_exempt: bool,
    depth: dict[str, Any],
    spike: float,
    config: dict[str, Any],
) -> tuple[float, list[str]]:
    multiplier = 1.0
    reasons: list[str] = []
    if pool == "observe_hot":
        hot_mult = float(config.get("sprint_hot_observe_risk_multiplier", 0.35))
        multiplier *= hot_mult
        reasons.append(f"热点观察小仓 {hot_mult:.2f}x")
    if mode_name == "tournament_sprint":
        ideal_max = float(config.get("sprint_atr_ideal_max_pct", 7.0))
        high = float(config.get("sprint_atr_high_pct", 10.0))
        if atr_pct > ideal_max:
            atr_mult = float(config.get("sprint_high_atr_risk_multiplier", 0.6))
            multiplier *= atr_mult
            reasons.append(f"高ATR降仓 {atr_mult:.2f}x")
        if atr_pct > high:
            depth_notional = float(depth.get("depth_notional", 0))
            extreme_depth = float(config.get("sprint_extreme_depth_notional_usdt", 50_000.0))
            if depth_notional < extreme_depth or spike < float(config.get("sprint_sample_penalty_exempt_spike", 2.5)):
                return 0.0, reasons + ["极端ATR且深度/放量不足，禁止"]
    if mode_name == "tournament_sprint" and sample_low and not sample_exempt:
        sample_mult = float(config.get("sprint_sample_low_risk_multiplier", 0.75))
        multiplier *= sample_mult
        reasons.append(f"样本偏少降仓 {sample_mult:.2f}x")
    return max(0.0, min(multiplier, 1.0)), reasons


def live_performance_summary(
    client: BinanceFuturesClient,
    symbol: str,
    direction: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    if not config.get("live_performance_boost_enabled", True):
        return {"enabled": False, "reason": "disabled"}
    if not getattr(client, "api_key", "") or not getattr(client, "api_secret", ""):
        return {"enabled": False, "reason": "missing_api"}

    window_ms = int(float(config.get("live_performance_window_hours", 36)) * 3_600_000)
    cutoff = int(time.time() * 1000) - window_ms
    try:
        raw_trades = client.user_trades(symbol, int(config.get("live_performance_trade_limit", 100)))
    except Exception as exc:
        return {"enabled": False, "reason": "fetch_failed", "error": str(exc)}

    direction = direction.upper()
    closing_orders: dict[str, dict[str, Any]] = {}
    total_commission = 0.0
    quote_qty = 0.0
    for trade in raw_trades:
        if int(trade.get("time", 0)) < cutoff:
            continue
        if str(trade.get("positionSide", "")).upper() != direction:
            continue
        commission = float(trade.get("commission") or 0)
        if str(trade.get("commissionAsset", "USDT")).upper() == "USDT":
            total_commission += commission
        quote_qty += float(trade.get("quoteQty") or 0)
        realized = float(trade.get("realizedPnl") or 0)
        if abs(realized) <= 0:
            continue
        order_id = str(trade.get("orderId") or trade.get("id"))
        item = closing_orders.setdefault(
            order_id,
            {"order_id": order_id, "realized_pnl": 0.0, "commission": 0.0, "fills": 0, "time": int(trade.get("time", 0))},
        )
        item["realized_pnl"] += realized
        item["commission"] += commission if str(trade.get("commissionAsset", "USDT")).upper() == "USDT" else 0.0
        item["fills"] += 1
        item["time"] = max(int(item["time"]), int(trade.get("time", 0)))

    closed = list(closing_orders.values())
    for item in closed:
        item["net_pnl"] = item["realized_pnl"] - item["commission"]
    wins = [item for item in closed if float(item["net_pnl"]) > 0]
    gross_profit = sum(float(item["net_pnl"]) for item in closed if float(item["net_pnl"]) > 0)
    gross_loss = abs(sum(float(item["net_pnl"]) for item in closed if float(item["net_pnl"]) < 0))
    realized_pnl = sum(float(item["realized_pnl"]) for item in closed)
    net_pnl = realized_pnl - total_commission
    profit_factor = gross_profit / gross_loss if gross_loss else (999.0 if gross_profit > 0 else 0.0)
    closed_trades = len(closed)
    passed = (
        closed_trades >= int(config.get("live_performance_min_closed_trades", 2))
        and net_pnl >= float(config.get("live_performance_min_net_pnl_usdt", 0.5))
        and profit_factor >= float(config.get("live_performance_min_profit_factor", 1.05))
    )
    return {
        "enabled": True,
        "passed": passed,
        "window_hours": float(config.get("live_performance_window_hours", 36)),
        "closed_trades": closed_trades,
        "wins": len(wins),
        "win_rate": len(wins) / closed_trades * 100 if closed_trades else 0.0,
        "realized_pnl_usdt": realized_pnl,
        "commission_usdt": total_commission,
        "net_pnl_usdt": net_pnl,
        "profit_factor": profit_factor,
        "quote_qty_usdt": quote_qty,
    }


def consecutive_live_losses(
    client: BinanceFuturesClient,
    symbols: list[str],
    direction: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    if not getattr(client, "api_key", "") or not getattr(client, "api_secret", ""):
        return {"enabled": False, "reason": "missing_api", "count": 0}
    try:
        income = client.income_history(int(config.get("live_performance_trade_limit", 100)), "REALIZED_PNL")
    except Exception as exc:
        return {"enabled": False, "reason": "fetch_failed", "error": str(exc), "count": 0}

    closing_orders = [
        {
            "symbol": str(item.get("symbol", "")),
            "net_pnl": float(item.get("income") or 0),
            "time": int(item.get("time", 0)),
        }
        for item in income
        if str(item.get("incomeType", "REALIZED_PNL")) == "REALIZED_PNL"
    ]
    closing_orders.sort(key=lambda item: int(item.get("time", 0)), reverse=True)
    count = 0
    for item in closing_orders:
        if float(item.get("net_pnl", 0)) < 0:
            count += 1
            continue
        break
    return {
        "enabled": True,
        "count": count,
        "recent": closing_orders[:5],
    }


def _apply_live_performance_quality(
    quality: dict[str, Any],
    live: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    if not live.get("passed"):
        return quality
    simulation = quality.get("simulation") or {}
    market = quality.get("market") or {}
    spread_ok = float(market.get("spread_pct", 999)) <= float(config.get("max_spread_pct", 0.08))
    depth_ok = float(market.get("depth_notional", 0)) >= float(config.get("live_performance_min_depth_notional_usdt", 1_500))
    sim_ok = (
        float(simulation.get("net_pct", 0)) >= float(config.get("live_performance_min_sim_net_pct", 5.0))
        and float(simulation.get("profit_factor", 0)) >= 1.0
        and int(simulation.get("trades", 0)) >= 1
    )
    if not (spread_ok and depth_ok and sim_ok):
        return {**quality, "live_performance": {**live, "adjusted": False}}

    adjusted = dict(quality)
    adjusted["score"] = round(min(100.0, float(adjusted.get("score", 0)) + min(12.0, float(live.get("net_pnl_usdt", 0)) * 3.0)), 2)
    adjusted["pool"] = "adaptive_live"
    adjusted["allowed"] = True
    adjusted["market_passed"] = True
    adjusted["live_performance"] = {**live, "adjusted": True}
    components = dict(adjusted.get("components") or {})
    components["live_performance"] = round(min(12.0, float(live.get("net_pnl_usdt", 0)) * 3.0), 2)
    adjusted["components"] = components
    return adjusted


def score_symbol_quality(
    symbol: str,
    bars: list[list[Any]],
    ticker: dict[str, Any],
    signal: dict[str, Any],
    backtests: dict[int, dict[str, Any]],
    depth: dict[str, Any],
    config: dict[str, Any],
    mode: dict[str, Any] | None = None,
) -> dict[str, Any]:
    mode_name = _mode_name(config, mode)
    weights = QUALITY_WEIGHTS[mode_name] if config.get("quality_mode_weights_enabled", True) else QUALITY_WEIGHTS["tournament"]
    quote_volume = float(ticker.get("quoteVolume", 0))
    volume_raw = _score_volume(quote_volume)
    spike = _volume_spike_ratio(bars)
    spike_raw = min(spike / max(float(config.get("volume_spike_ratio", 1.8)), 0.1), 1.0) * 15.0
    price = float(signal.get("last_price") or ticker.get("lastPrice") or bars[-1][4] or 0)
    atr_pct = float(signal.get("atr") or 0) / price * 100 if price else 0.0
    volatility_raw = _score_volatility(atr_pct)
    volatility_mode_ratio = _score_volatility_for_mode(atr_pct, mode_name, config)
    spread_depth_raw = _score_spread_depth(depth, config)
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
    sample_low = int(primary.get("trades", 0)) < int(config.get("min_simulated_trades", 5))
    sample_exempt = (
        mode_name == "tournament_sprint"
        and spike >= float(config.get("sprint_sample_penalty_exempt_spike", 2.5))
        and bool(signal.get("trend") is True or signal.get("signal") in {"LONG", "SHORT"})
        and float(depth.get("depth_notional", 0)) >= float(config.get("min_depth_notional_usdt", 20_000))
    )
    if sample_low and not sample_exempt:
        false_breakout_penalty += (
            float(config.get("sprint_sample_penalty", 1.0))
            if mode_name == "tournament_sprint"
            else {"conservative": 10.0, "balanced": 8.0, "attack": 5.0, "tournament": 3.0}.get(mode_name, 8.0)
        )
    if float(primary.get("win_rate", 0)) < 35 and int(primary.get("trades", 0)) >= 5:
        false_breakout_penalty += 8.0
    high_atr_penalty = 0.0
    if mode_name != "tournament_sprint" and atr_pct > 5.5:
        high_atr_penalty = 6.0
    elif mode_name == "tournament_sprint" and atr_pct > float(config.get("sprint_atr_high_pct", 10.0)):
        high_atr_penalty = 2.0
    if high_atr_penalty:
        false_breakout_penalty += high_atr_penalty
    component_ratios = {
        "volume": min(volume_raw / 15.0, 1.0),
        "volume_spike": min(spike_raw / 15.0, 1.0),
        "volatility": volatility_mode_ratio,
        "spread_depth": min(spread_depth_raw / 15.0, 1.0),
        "backtest_3d": min(bt3_score / 15.0, 1.0),
        "backtest_5d": min(bt5_score / 10.0, 1.0),
        "backtest_10d": min(bt10_score / 5.0, 1.0) if bt10_score else 0.0,
        "trend": min(trend_score / 5.0, 1.0),
    }
    weighted_components = {
        key: component_ratios[key] * weight
        for key, weight in weights.items()
    }
    score = sum(weighted_components.values()) - min(false_breakout_penalty, 20.0)
    score = max(0.0, min(score, 100.0))
    trade_score = float(config.get("sprint_symbol_trade_score", 68.0) if mode_name == "tournament_sprint" else config.get("symbol_trade_score", 75.0))
    small_score = float(config.get("sprint_symbol_small_trade_score", 55.0) if mode_name == "tournament_sprint" else config.get("symbol_small_trade_score", 65.0))
    hot_score = float(config.get("sprint_symbol_hot_observe_score", 45.0))
    observe_score = float(config.get("symbol_observe_score", 50.0))
    market_passed = (
        float(depth.get("spread_pct", 999)) <= float(config.get("max_spread_pct", 0.08))
        and float(depth.get("depth_notional", 0)) >= float(config.get("min_depth_notional_usdt", 20_000))
    )
    hot_observe = (
        mode_name == "tournament_sprint"
        and score >= hot_score
        and market_passed
        and spike >= float(config.get("sprint_sample_penalty_exempt_spike", 2.5))
        and bool(signal.get("trend") is True or signal.get("signal") in {"LONG", "SHORT"})
    )
    if score >= trade_score and simulation["passed"] and market_passed:
        pool = "trade"
        allowed = True
    elif score >= small_score and simulation["passed"] and market_passed:
        pool = "small_trade"
        allowed = True
    elif hot_observe:
        pool = "observe_hot"
        allowed = True
    elif score >= observe_score:
        pool = "observe"
        allowed = False
    else:
        pool = "disabled"
        allowed = False
    quality_multiplier, quality_reasons = _quality_risk_multiplier(
        mode_name=mode_name,
        pool=pool,
        atr_pct=atr_pct,
        sample_low=sample_low,
        sample_exempt=sample_exempt,
        depth=depth,
        spike=spike,
        config=config,
    )
    if quality_multiplier <= 0:
        allowed = False
    return {
        "score": round(score, 2),
        "pool": pool,
        "allowed": allowed,
        "quality_risk_multiplier": round(quality_multiplier, 4),
        "quality_risk_reasons": quality_reasons,
        "mode": mode_name,
        "components": {
            "volume": round(weighted_components["volume"], 2),
            "volume_spike": round(weighted_components["volume_spike"], 2),
            "volatility": round(weighted_components["volatility"], 2),
            "spread_depth": round(weighted_components["spread_depth"], 2),
            "backtest_3d": round(weighted_components["backtest_3d"], 2),
            "backtest_5d": round(weighted_components["backtest_5d"], 2),
            "backtest_10d": round(weighted_components["backtest_10d"], 2),
            "trend": round(weighted_components["trend"], 2),
            "false_breakout_penalty": round(false_breakout_penalty, 2),
            "sample_penalty": round(0.0 if (not sample_low or sample_exempt) else false_breakout_penalty, 2),
            "raw_volume": round(volume_raw, 2),
            "raw_volume_spike": round(spike_raw, 2),
            "raw_volatility": round(volatility_raw, 2),
            "raw_spread_depth": round(spread_depth_raw, 2),
        },
        "penalties": {
            "sample_low": sample_low,
            "sample_exempt": sample_exempt,
            "high_atr_penalty": round(high_atr_penalty, 2),
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


def observe_breakout_allows_entry(
    candidate_score: float,
    quality: dict[str, Any],
    recent: dict[str, Any],
    signal: dict[str, Any],
    cost_ratio: float,
    depth: dict[str, Any],
    config: dict[str, Any],
    mode: dict[str, Any],
) -> bool:
    if not config.get("observe_breakout_enabled", True):
        return False
    if mode.get("mode") not in {"tournament", "tournament_sprint"}:
        return False
    if quality.get("pool") != "observe":
        return False
    if signal.get("signal") not in {"LONG", "SHORT"}:
        return False
    if mode.get("mode") == "tournament_sprint":
        if candidate_score < float(config.get("tournament_sprint_standard_min_score", config.get("standard_min_score", 85.0))):
            return False
        if float(quality.get("score", 0)) < float(config.get("observe_breakout_min_quality", 78.0)) - 5.0:
            return False
        if cost_ratio < float(config.get("tournament_sprint_min_expected_profit_cost_ratio", 1.35)):
            return False
        if float(recent.get("profit_factor", 0)) < float(config.get("tournament_sprint_long_min_profit_factor", 0.85)):
            return False
        if float(recent.get("net_pct", 0)) < float(config.get("tournament_sprint_long_min_net_pct", -3.0)):
            return False
        if float(depth.get("spread_pct", 999)) > float(config.get("observe_breakout_max_spread_pct", 0.08)):
            return False
        if float(depth.get("depth_notional", 0)) < float(config.get("observe_breakout_min_depth_notional_usdt", 500.0)):
            return False
        return True
    if candidate_score < float(config.get("observe_breakout_min_score", 105.0)):
        return False
    if float(quality.get("score", 0)) < float(config.get("observe_breakout_min_quality", 78.0)):
        return False
    if cost_ratio < float(config.get("observe_breakout_min_cost_ratio", 20.0)):
        return False
    if float(recent.get("profit_factor", 0)) < float(config.get("observe_breakout_min_profit_factor", 1.5)):
        return False
    if float(recent.get("net_pct", 0)) < float(config.get("observe_breakout_min_net_pct", 4.0)):
        return False
    if float(depth.get("spread_pct", 999)) > float(config.get("observe_breakout_max_spread_pct", 0.08)):
        return False
    if float(depth.get("depth_notional", 0)) < float(config.get("observe_breakout_min_depth_notional_usdt", 500.0)):
        return False
    atr_pct = float((quality.get("market") or {}).get("atr_pct", 0))
    extreme_atr = float(config.get("observe_extreme_atr_pct", 3.0))
    extreme_depth = float(config.get("observe_extreme_depth_notional_usdt", 5_000.0))
    if atr_pct >= extreme_atr and float(depth.get("depth_notional", 0)) < extreme_depth:
        return False
    return True


def observe_breakout_risk_adjustment(
    quality: dict[str, Any],
    signal: dict[str, Any],
    live_losses: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    multiplier = float(config.get("observe_breakout_risk_multiplier", 0.22))
    reasons = [f"观察池基础折扣 {multiplier:.2f}"]
    price = float(signal.get("last_price") or 0)
    low_price_threshold = float(config.get("observe_low_price_threshold", 0.01))
    if price and low_price_threshold > 0 and price < low_price_threshold:
        low_mult = float(config.get("observe_low_price_risk_multiplier", 0.75))
        multiplier *= low_mult
        reasons.append(f"低价币折扣 {low_mult:.2f}")
    atr_pct = float((quality.get("market") or {}).get("atr_pct", 0))
    if atr_pct >= float(config.get("observe_high_atr_pct", 3.0)):
        atr_mult = float(config.get("observe_high_atr_risk_multiplier", 0.75))
        multiplier *= atr_mult
        reasons.append(f"高ATR折扣 {atr_mult:.2f}")
    loss_count = int(live_losses.get("count", 0) or 0)
    threshold = int(config.get("observe_consecutive_loss_count", 2))
    if threshold > 0 and loss_count >= threshold:
        loss_mult = float(config.get("observe_consecutive_loss_risk_multiplier", 0.5))
        multiplier *= loss_mult
        reasons.append(f"连续亏损{loss_count}笔折扣 {loss_mult:.2f}")
    return {"multiplier": multiplier, "reasons": reasons, "live_losses": live_losses}


def backtest_strategy(
    symbol: str,
    bars: list[list[Any]],
    strategy: str,
    days: int,
    direction: str = "LONG",
    params: StrategyParams | None = None,
) -> dict[str, Any]:
    if len(bars) < 100:
        return {"symbol": symbol, "trades": 0, "wins": 0, "win_rate": 0, "net_pct": 0, "profit_factor": 0}

    custom_params = params is not None
    params = params or StrategyParams()
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
            stop_mult = params.stop_atr if custom_params else 1.2
            take_mult = params.take_profit_atr if custom_params else 2.5
            max_hold = params.max_hold_bars if custom_params else 10
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
    started_at = time.perf_counter()
    equity = account_summary.get("equity")
    mode = mode_config(config, equity)
    limits = pipeline_limits(config, mode)
    symbols = discover_coin_symbols(client, config)
    candidates = []
    recalled_symbols = list(symbols)
    tickers = {item["symbol"]: item for item in client.ticker_24h(recalled_symbols)}
    ranked_symbols, coarse_rows = _coarse_rank_symbols(recalled_symbols, tickers, config, mode)
    fee_pct = StrategyParams().taker_fee * 2 * 100
    slippage_pct = float(config.get("estimated_slippage_pct", 0.04))
    cost_pct = fee_pct + slippage_pct
    quality_days = sorted({
        int(day)
        for day in config.get("quality_backtest_days", [3, 5])
        if int(day) > 0
    } | {int(mode["recent_days"])})
    max_depth_checks = min(int(config.get("depth_check_top_symbols", 8)), limits["auction"])
    degrade_seconds = float(config.get("scan_degrade_seconds", 18))
    min_rank_symbols = min(int(config.get("scan_min_rank_symbols", 8)), len(ranked_symbols))
    depth_checks = 0
    depth_by_symbol: dict[str, dict[str, Any]] = {}
    live_losses_by_direction: dict[str, dict[str, Any]] = {}
    processed_symbols: list[str] = []

    for symbol in ranked_symbols:
        if len(processed_symbols) >= min_rank_symbols and time.perf_counter() - started_at >= degrade_seconds:
            break
        processed_symbols.append(symbol)
        try:
            bars = client.klines_history(symbol, mode["interval"], max(quality_days))
            directions = ["LONG", "SHORT"] if config.get("allow_short", False) else ["LONG"]
            for direction in directions:
                is_sprint = mode["mode"] == "tournament_sprint"
                signal_params = strategy_params_for_mode(config, mode, "standard")
                signal = latest_strategy_signal(symbol, bars, mode["strategy"], params=signal_params, direction=direction)
                backtests = {
                    day: _backtest_strategy_with_params(symbol, bars, mode["strategy"], day, direction, signal_params)
                    for day in quality_days
                }
                recent = backtests[int(mode["recent_days"])]
                ticker = tickers.get(symbol, {})
                last_price = float(signal.get("last_price") or ticker.get("lastPrice", 0))
                expected_profit_pct = float(signal.get("expected_profit_pct") or 0)
                cost_ratio = expected_profit_pct / cost_pct if cost_pct else 0

                if direction == "SHORT":
                    min_trades = int(config.get("tournament_sprint_short_min_recent_trades", 3) if is_sprint else config.get("short_min_recent_trades", 5))
                    min_pf = float(config.get("tournament_sprint_short_min_profit_factor", 1.05) if is_sprint else config.get("short_min_profit_factor", 1.3))
                    min_net_pct = float(config.get("tournament_sprint_short_min_net_pct", -2.0) if is_sprint else config.get("short_min_net_pct", 1.0))
                    risk_pct = float(mode["risk_pct"]) * float(config.get("short_risk_multiplier", 0.5))
                else:
                    min_trades = int(mode["min_trades"])
                    min_pf = float(config.get("tournament_sprint_long_min_profit_factor", mode["min_pf"]) if is_sprint else mode["min_pf"])
                    min_net_pct = float(config.get("tournament_sprint_long_min_net_pct", -3.0) if is_sprint else 0.0)
                    risk_pct = float(mode["risk_pct"])
                if direction not in live_losses_by_direction:
                    live_losses_by_direction[direction] = consecutive_live_losses(client, processed_symbols or ranked_symbols, direction, config)

                current_score = _current_signal_score(signal, direction, cost_ratio, recent)
                should_check_depth = (
                    max_depth_checks > 0
                    and depth_checks < max_depth_checks
                    and (
                        signal.get("signal") == direction
                        or current_score >= float(config.get("depth_check_min_current_score", 38.0))
                    )
                )
                if should_check_depth and symbol not in depth_by_symbol:
                    depth_by_symbol[symbol] = _depth_metrics(client, symbol)
                    depth_checks += 1
                depth = depth_by_symbol.get(symbol, _unchecked_depth_metrics())
                should_check_live = (
                    signal.get("signal") == direction
                    or current_score >= float(config.get("live_performance_check_min_score", 70.0))
                )
                live_perf = (
                    live_performance_summary(client, symbol, direction, config)
                    if should_check_live
                    else {"enabled": False, "reason": "candidate_score_low"}
                )
                quality = score_symbol_quality(symbol, bars, ticker, signal, backtests, depth, config, mode)
                quality = _apply_live_performance_quality(quality, live_perf, config)
                quality_multiplier = float(quality.get("quality_risk_multiplier", 1.0))
                risk_pct *= quality_multiplier
                history_passed = (
                    recent["trades"] >= min_trades
                    and recent["profit_factor"] >= min_pf
                    and recent["net_pct"] > min_net_pct
                )
                standard_passed = (
                    signal.get("signal") == direction
                    and history_passed
                    and quality["allowed"]
                    and expected_profit_pct >= float(config.get("tournament_sprint_min_expected_profit_pct", 0.22) if is_sprint else config.get("min_expected_profit_pct", 0.35))
                    and cost_ratio >= float(config.get("tournament_sprint_min_expected_profit_cost_ratio", 1.35) if is_sprint else config.get("min_expected_profit_cost_ratio", 3.0))
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
                standard_min_score = float(config.get("tournament_sprint_standard_min_score", 72.0) if is_sprint else config.get("standard_min_score", 85.0))
                passed = standard_passed and score >= standard_min_score
                risk_adjustment: dict[str, Any] | None = None
                decision_reason = "标准突破信号通过" if passed else "等待触发"
                if passed and quality["pool"] == "small_trade":
                    entry_type = "small_standard"
                    risk_pct *= float(config.get("small_trade_risk_multiplier", 0.5))
                    decision_reason = "币种质量允许小仓试探"
                if passed and quality["pool"] == "adaptive_live":
                    entry_type = "adaptive_live_standard"
                    risk_pct *= float(config.get("live_performance_risk_multiplier", 0.6))
                    decision_reason = "recent live performance supports reduced-risk entry"
                if signal.get("signal") == direction and not quality["allowed"]:
                    if observe_breakout_allows_entry(score, quality, recent, signal, cost_ratio, depth, config, mode):
                        entry_type = "observe_standard"
                        passed = score >= standard_min_score
                        risk_adjustment = observe_breakout_risk_adjustment(
                            quality,
                            signal,
                            live_losses_by_direction.get(direction, {"enabled": False, "count": 0}),
                            config,
                        )
                        risk_pct *= float(risk_adjustment["multiplier"])
                        decision_reason = "观察池高分标准突破，允许折扣仓位试单" if passed else "观察池标准突破评分不足"
                    else:
                        decision_reason = f"币种质量未达实盘准入：{quality['pool']}，评分 {quality['score']}"
                preemptive_enabled = mode["mode"] in {"tournament", "tournament_sprint"} and config.get("preemptive_entries_enabled", True)
                if not passed and preemptive_enabled and history_passed and quality["allowed"] and signal.get("signal") == "WAIT":
                    distance_pct = float(signal.get("distance_to_trigger_pct") or 999)
                    max_distance = float(config.get("tournament_sprint_preemptive_max_distance_pct", 0.55) if is_sprint else config.get("preemptive_max_distance_pct", 0.35))
                    min_candle_pct = float(config.get("tournament_sprint_momentum_min_candle_pct", 0.10)) if is_sprint else 0.12
                    near_trigger = (
                        signal.get("trend") is True
                        and signal.get("volatility_ok") is True
                        and distance_pct <= max_distance
                    )
                    strong_momentum = (
                        (config.get("tournament_sprint_momentum_enabled", True) if is_sprint else True)
                        and signal.get("trend") is True
                        and signal.get("volatility_ok") is True
                        and float(signal.get("candle_move_pct") or 0) >= max(min_candle_pct, distance_pct)
                    )
                    min_preempt_score = float(config.get("tournament_sprint_preemptive_min_score", 58.0) if is_sprint else config.get("preemptive_min_score", 72.0))
                    min_momentum_score = float(config.get("tournament_sprint_momentum_min_score", min_preempt_score) if is_sprint else min_preempt_score)
                    if (near_trigger and score >= min_preempt_score) or (strong_momentum and score >= min_momentum_score):
                        entry_type = "momentum" if strong_momentum and not near_trigger else "preemptive"
                        risk_multiplier = (
                            float(config.get("tournament_sprint_short_preemptive_risk_multiplier", 0.25) if is_sprint else config.get("short_preemptive_risk_multiplier", 0.18))
                            if direction == "SHORT"
                            else float(config.get("tournament_sprint_preemptive_risk_multiplier", 0.35) if is_sprint else config.get("preemptive_risk_multiplier", 0.24))
                        )
                        if quality["pool"] == "small_trade":
                            risk_multiplier *= float(config.get("small_trade_risk_multiplier", 0.5))
                        entry_params = strategy_params_for_mode(config, mode, entry_type)
                        signal = _promote_wait_signal(signal, direction, entry_type, risk_multiplier, params=entry_params)
                        expected_profit_pct = float(signal.get("expected_profit_pct") or 0)
                        cost_ratio = expected_profit_pct / cost_pct if cost_pct else 0
                        risk_pct *= risk_multiplier
                        passed = (
                            expected_profit_pct >= float(config.get("tournament_sprint_min_expected_profit_pct", 0.22) if is_sprint else config.get("min_expected_profit_pct", 0.35))
                            and cost_ratio >= (
                                float(config.get("tournament_sprint_min_expected_profit_cost_ratio", 1.35))
                                if is_sprint
                                else max(1.5, float(config.get("min_expected_profit_cost_ratio", 3.0)) * 0.65)
                            )
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
                        if distance_pct > max_distance:
                            misses.append("距离触发价偏远")
                        if score < min_preempt_score:
                            misses.append("综合评分不足")
                        decision_reason = "、".join(misses) or "等待触发"

                candidate = {
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
                        "live_performance": live_perf,
                        "depth_checked": depth.get("reason") != "depth_not_checked",
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
                        "risk_adjustment": risk_adjustment,
                        "quality_risk_multiplier": quality.get("quality_risk_multiplier", 1.0),
                        "quality_risk_reasons": quality.get("quality_risk_reasons", []),
                        "base_risk_pct": mode["risk_pct"],
                        "leverage": mode["leverage"],
                        "margin_pct": mode["margin_pct"],
                        "thresholds": {"min_trades": min_trades, "min_pf": min_pf, "min_net_pct": min_net_pct},
                        "coarse": next((row for row in coarse_rows if row["symbol"] == symbol), {}),
                    }
                candidate = apply_live_credit_to_candidate(candidate, config)
                candidates.append(candidate)
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
        if candidate.get("symbol_quality", {}).get("pool") in {"trade", "small_trade", "adaptive_live", "observe_hot"}
    ][: int(config.get("max_trade_pool_symbols", 15))]
    observe_pool = [
        candidate
        for candidate in candidates
        if candidate.get("symbol_quality", {}).get("pool") == "observe"
    ][:max_candidates]
    funnel = {
        "recall": {
            "count": len(recalled_symbols),
            "limit": limits["recall"],
            "label": "大召回",
        },
        "coarse": {
            "count": len(coarse_rows),
            "limit": limits["coarse"],
            "label": "粗排",
        },
        "rank": {
            "count": len(processed_symbols),
            "limit": limits["rank"],
            "label": "精排",
            "planned": len(ranked_symbols),
            "degraded": len(processed_symbols) < len(ranked_symbols),
        },
        "auction": {
            "count": depth_checks,
            "limit": max_depth_checks,
            "label": "竞价",
        },
        "candidates": {
            "count": len(candidates),
            "displayed": min(max_candidates, len(candidates)),
            "label": "候选",
        },
        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
        "degrade_seconds": degrade_seconds,
        "coarse_top": _json_safe(coarse_rows[:20]),
    }
    return {
        "mode": _json_safe(mode),
        "symbols": processed_symbols,
        "ranked_symbols": ranked_symbols,
        "recalled_symbols": recalled_symbols,
        "funnel": _json_safe(funnel),
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
