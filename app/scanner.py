from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import time
from typing import Any

from app.adaptive_thresholds import adaptive_thresholds, build_market_profile
from app.binance_client import BinanceFuturesClient
from app.exchange_filters import ExchangeFilters
from app.live_learning import apply_live_credit_to_candidate, list_live_scores
from app.live_reaction import apply_live_reaction_to_candidate
from app.performance_guard import apply_strategy_evidence_to_candidate, observed_round_trip_cost_pct
from app.strategy_calibration import calibrate_v3_opportunity
from app.market_structure import build_market_structure_features, market_structure
from app.market_stream import stream_depth, stream_triggers, write_stream_intent
from app.opportunity_engine import (
    V3_STRATEGY_FAMILY,
    build_market_context,
    build_v31_medium_context,
    build_v33_challenger,
    build_v3_signal,
    score_v3_opportunity,
)
from app.opportunity_queue import read_opportunities
from app.opportunity_v4 import V4_STRATEGY_FAMILY, attach_v4_rankings
from app.position_sizing import effective_position_risk
from app.s0_full_bet import s0_full_bet_profile_active
from app.scalp_engine import build_scalp_signal
from app.shadow_trading import active_shadow_symbols
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
    "extreme_sprint": {
        "strategy": "breakout",
        "interval_key": "extreme_sprint_interval",
        "recent_days_key": "extreme_sprint_recent_days",
        "risk_key": "extreme_sprint_risk_per_trade_pct",
        "leverage_key": "extreme_sprint_max_leverage",
        "margin_key": "extreme_sprint_max_symbol_margin_pct",
        "min_pf": 0.75,
        "min_trades": 1,
        "recent_days": 2,
    },
    "yolo_scalp": {
        "strategy": "breakout",
        "interval_key": "yolo_scalp_interval",
        "recent_days_key": "yolo_scalp_recent_days",
        "risk_key": "yolo_scalp_risk_per_trade_pct",
        "leverage_key": "yolo_scalp_max_leverage",
        "margin_key": "yolo_scalp_max_symbol_margin_pct",
        "min_pf": 0.55,
        "min_trades": 1,
        "recent_days": 1,
    },
}


PIPELINE_DEFAULTS: dict[str, dict[str, int]] = {
    "conservative": {"recall": 180, "coarse": 70, "rank": 45, "auction": 8},
    "balanced": {"recall": 250, "coarse": 100, "rank": 60, "auction": 10},
    "attack": {"recall": 350, "coarse": 140, "rank": 70, "auction": 12},
    "tournament": {"recall": 500, "coarse": 180, "rank": 80, "auction": 15},
    "tournament_sprint": {"recall": 600, "coarse": 220, "rank": 90, "auction": 15},
    "extreme_sprint": {"recall": 650, "coarse": 240, "rank": 110, "auction": 18},
    "yolo_scalp": {"recall": 700, "coarse": 260, "rank": 120, "auction": 20},
}

_V31_BAR_CACHE: dict[str, tuple[float, list[list[Any]]]] = {}


def _v31_medium_bars(
    client: BinanceFuturesClient,
    symbols: list[str],
    config: dict[str, Any],
) -> dict[str, list[list[Any]]]:
    """Load a small cached 1h shortlist; never fan out across the recall universe."""
    if not (
        config.get("opportunity_v33_challenger_enabled", True)
        or config.get("opportunity_v31_challenger_enabled", False)
        or config.get("opportunity_v4_enabled", True)
    ):
        return {}
    ttl = max(60, int(config.get("opportunity_v31_bar_cache_seconds", 600)))
    limit = max(1, int(config.get("opportunity_v31_medium_pool_limit", 12)))
    now = time.monotonic()
    result: dict[str, list[list[Any]]] = {}
    for symbol in symbols[:limit]:
        cached = _V31_BAR_CACHE.get(symbol)
        if cached and now - cached[0] <= ttl:
            result[symbol] = cached[1]
            continue
        try:
            bars = client.klines(symbol, "1h", 200)
        except Exception:
            continue
        if len(bars) >= 80:
            _V31_BAR_CACHE[symbol] = (now, bars)
            result[symbol] = bars
    if len(_V31_BAR_CACHE) > limit * 4:
        for key, value in list(_V31_BAR_CACHE.items()):
            if now - value[0] > ttl * 2:
                _V31_BAR_CACHE.pop(key, None)
    return result


def extreme_sprint_armed(config: dict[str, Any]) -> bool:
    return (
        config.get("extreme_sprint_enabled") is True
        and str(config.get("extreme_sprint_confirmation", "")) == "ENABLE_EXTREME_SPRINT"
    )


def yolo_scalp_armed(config: dict[str, Any]) -> bool:
    return (
        config.get("yolo_scalp_enabled") is True
        and str(config.get("yolo_scalp_confirmation", "")) == "ENABLE_YOLO_SCALP"
    )


def is_extreme_mode(mode_name: str | None) -> bool:
    return str(mode_name or "").lower() in {"extreme_sprint", "yolo_scalp"}


def opportunity_v3_active(config: dict[str, Any], mode_name: str | None) -> bool:
    return bool(config.get("opportunity_v3_enabled", False) and str(mode_name or "").lower() == "extreme_sprint")


def active_growth_mode(config: dict[str, Any], equity: float | None = None) -> str:
    routed = str(config.get("_active_growth_mode") or "").lower()
    if routed in MODE_PRESETS:
        return routed
    configured = str(config.get("growth_mode", "balanced")).lower()
    if configured == "yolo_scalp":
        return "yolo_scalp" if yolo_scalp_armed(config) else "balanced"
    if configured == "extreme_sprint":
        return "extreme_sprint" if extreme_sprint_armed(config) else "balanced"
    if not config.get("auto_risk_by_equity", True):
        return configured if configured in MODE_PRESETS else "balanced"
    if equity is None:
        return configured if configured in MODE_PRESETS else "balanced"
    if yolo_scalp_armed(config) and equity < float(config.get("yolo_scalp_auto_under_equity", 300.0)):
        return "yolo_scalp"
    if extreme_sprint_armed(config) and equity < float(config.get("extreme_sprint_auto_under_equity", 10_000.0)):
        return "extreme_sprint"
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
    route = config.get("_stage_route") or {}
    if route.get("mode") == mode:
        preset["risk_pct"] = float(route.get("risk_pct", preset["risk_pct"]))
        preset["leverage"] = float(route.get("leverage", preset["leverage"]))
        preset["margin_pct"] = float(route.get("margin_pct", preset["margin_pct"]))
        preset["stage"] = route.get("stage")
        preset["strategy_family"] = route.get("strategy_family")
        preset["max_open_positions"] = int(route.get("max_open_positions", 1))
        preset["daily_loss_limit_pct"] = float(
            route.get("daily_loss_limit_pct", config.get("daily_loss_limit_pct", 3.0))
        )
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
    if mode_name not in {"tournament_sprint", "extreme_sprint", "yolo_scalp"}:
        return None
    entry_key = "momentum" if entry_type == "momentum" else "preemptive" if entry_type in {"preemptive", "extreme_probe", "weak_quality_probe"} else "standard"
    if mode_name == "yolo_scalp":
        defaults = {
            "standard": (0.38, 0.55, 2),
            "preemptive": (0.32, 0.48, 2),
            "momentum": (0.35, 0.60, 2),
        }
    elif mode_name == "extreme_sprint":
        defaults = {
            "standard": (0.85, 1.80, 10),
            "preemptive": (0.70, 1.15, 5),
            "momentum": (0.75, 1.35, 6),
        }
    else:
        defaults = {
            "standard": (0.9, 1.4, 6),
            "preemptive": (0.75, 1.0, 4),
            "momentum": (0.8, 1.2, 5),
        }
    default_stop, default_take, default_hold = defaults[entry_key]
    prefix = "yolo_scalp" if mode_name == "yolo_scalp" else "extreme_sprint" if mode_name == "extreme_sprint" else "tournament_sprint"
    if is_extreme_mode(mode_name) and entry_type == "weak_quality_probe":
        return StrategyParams(
            stop_atr=float(config.get(f"{prefix}_weak_probe_stop_atr", config.get("weak_quality_probe_stop_atr", default_stop))),
            take_profit_atr=float(config.get(f"{prefix}_weak_probe_take_profit_atr", config.get("weak_quality_probe_take_profit_atr", default_take))),
            max_hold_bars=int(config.get(f"{prefix}_weak_probe_max_hold_bars", config.get("weak_quality_probe_max_hold_bars", default_hold))),
            min_atr_pct=0.006,
        )
    if is_extreme_mode(mode_name) and entry_type == "extreme_probe":
        return StrategyParams(
            stop_atr=float(config.get(f"{prefix}_probe_stop_atr", config.get("extreme_probe_stop_atr", default_stop))),
            take_profit_atr=float(config.get(f"{prefix}_probe_take_profit_atr", config.get("extreme_probe_take_profit_atr", default_take))),
            max_hold_bars=int(config.get(f"{prefix}_probe_max_hold_bars", config.get("extreme_probe_max_hold_bars", default_hold))),
            min_atr_pct=0.006,
        )
    return StrategyParams(
        stop_atr=float(config.get(f"{prefix}_{entry_key}_stop_atr", default_stop)),
        take_profit_atr=float(config.get(f"{prefix}_{entry_key}_take_profit_atr", default_take)),
        max_hold_bars=int(config.get(f"{prefix}_{entry_key}_max_hold_bars", default_hold)),
        min_atr_pct=0.006,
    )


def _mode_key(mode: dict[str, Any], sprint_key: str, extreme_key: str) -> str:
    return extreme_key if is_extreme_mode(mode.get("mode")) else sprint_key


def classify_market_state(
    symbol: str,
    bars: list[list[Any]],
    signal: dict[str, Any],
    depth: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    if not config.get("market_state_filter_enabled", True):
        return {"state": "neutral", "label": "未启用行情分类", "allows_entry": True, "risk_multiplier": 1.0}
    if len(bars) < 5:
        return {"state": "insufficient_data", "label": "K线不足", "allows_entry": False, "risk_multiplier": 0.0}
    last = bars[-1]
    try:
        open_price = float(last[1])
        high = float(last[2])
        low = float(last[3])
        close = float(last[4])
    except (TypeError, ValueError, IndexError):
        return {"state": "bad_kline", "label": "K线异常", "allows_entry": False, "risk_multiplier": 0.0}
    body = abs(close - open_price)
    upper_wick = max(0.0, high - max(open_price, close))
    lower_wick = max(0.0, min(open_price, close) - low)
    wick_ratio = max(upper_wick, lower_wick) / max(body, close * 0.0001, 0.00000001)
    candle_move_pct = abs(close - open_price) / close * 100 if close else 0.0
    atr_pct = float(signal.get("atr") or 0) / close * 100 if close else 0.0
    volume_spike = _volume_spike_ratio(bars)
    adaptive = adaptive_thresholds(bars, config)
    spread_pct = float(depth.get("spread_pct") or 999)
    depth_notional = float(depth.get("depth_notional") or 0)
    max_spread = float(config.get("max_spread_pct", 0.08))
    min_depth = float(config.get("market_state_trap_max_depth_notional_usdt", 800))
    depth_known = depth.get("reason") != "depth_not_checked" and depth.get("available", True) is not False
    if depth_known and (spread_pct > max_spread * 2 or (depth_notional and depth_notional < min_depth)):
        return {
            "state": "liquidity_trap",
            "label": "盘口薄/价差大",
            "allows_entry": False,
            "risk_multiplier": 0.0,
            "wick_ratio": round(wick_ratio, 4),
            "volume_spike": round(volume_spike, 4),
            "atr_pct": round(atr_pct, 4),
        }
    if wick_ratio >= float(config.get("market_state_spike_wick_ratio", 2.2)) and candle_move_pct >= 0.25:
        return {
            "state": "spike_wick",
            "label": "插针风险",
            "allows_entry": False,
            "risk_multiplier": 0.0,
            "wick_ratio": round(wick_ratio, 4),
            "volume_spike": round(volume_spike, 4),
            "atr_pct": round(atr_pct, 4),
        }
    trend_volume_min = max(
        float(config.get("market_state_min_volume_spike", 1.2)),
        float(adaptive.get("volume_spike_min", 1.2)) * 0.80,
    )
    if signal.get("trend") and signal.get("volatility_ok") and volume_spike >= trend_volume_min:
        return {
            "state": "trend_breakout",
            "label": "趋势放量",
            "allows_entry": True,
            "risk_multiplier": 1.12,
            "wick_ratio": round(wick_ratio, 4),
            "volume_spike": round(volume_spike, 4),
            "atr_pct": round(atr_pct, 4),
            "adaptive_thresholds": adaptive,
        }
    if signal.get("trend") and signal.get("volatility_ok"):
        return {
            "state": "trend_continuation",
            "label": "趋势延续",
            "allows_entry": True,
            "risk_multiplier": 1.0,
            "wick_ratio": round(wick_ratio, 4),
            "volume_spike": round(volume_spike, 4),
            "atr_pct": round(atr_pct, 4),
            "adaptive_thresholds": adaptive,
        }
    return {
        "state": "chop",
        "label": "震荡等待",
        "allows_entry": False,
        "risk_multiplier": 0.0,
        "wick_ratio": round(wick_ratio, 4),
        "volume_spike": round(volume_spike, 4),
        "atr_pct": round(atr_pct, 4),
        "adaptive_thresholds": adaptive,
    }


def execution_viability(
    filters: ExchangeFilters | None,
    symbol: str,
    equity: float | None,
    risk_pct: float,
    entry: float,
    stop: float,
    max_notional: float,
) -> dict[str, Any]:
    if filters is None:
        return {"enabled": False, "executable": True}
    if equity is None or entry <= 0 or stop <= 0:
        return {"enabled": True, "executable": False, "reason": "account_or_price_missing"}
    raw_qty = (float(equity) * float(risk_pct) / 100) / abs(entry - stop) if abs(entry - stop) > 0 else 0.0
    cap_qty = max_notional / entry if entry > 0 else 0.0
    quantity = filters.quantity(symbol, min(raw_qty, cap_qty))
    notional = quantity * entry
    min_notional = filters.min_notional(symbol)
    executable = quantity > 0 and notional >= min_notional
    return {
        "enabled": True,
        "executable": executable,
        "quantity": quantity,
        "notional": round(notional, 8),
        "min_notional": min_notional,
        "raw_quantity": raw_qty,
        "max_quantity": cap_qty,
        "reason": "ok" if executable else "below_min_order",
    }


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
    if is_extreme_mode(active_growth_mode(config)) and config.get("extreme_v2_enabled", True):
        min_volume = min(min_volume, float(config.get("extreme_firecracker_min_quote_volume_usdt", 30_000_000)))
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
            "trend": trend,
            "trigger": trigger,
            "volatility_ok": volatility_ok,
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


def trend_pullback_signal(
    symbol: str,
    bars: list[list[Any]],
    direction: str,
    params: StrategyParams | None = None,
) -> dict[str, Any] | None:
    """Confirm a trend continuation after a controlled EMA pullback."""
    if len(bars) < 80:
        return None
    params = params or StrategyParams(stop_atr=0.85, take_profit_atr=1.80, max_hold_bars=10)
    closes = [float(row[4]) for row in bars]
    highs = [float(row[2]) for row in bars]
    lows = [float(row[3]) for row in bars]
    e10 = ema(closes, 10)
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    atr_value = float(atr(bars, params.atr_period)[-1] or 0)
    i = len(bars) - 1
    close = closes[i]
    is_short = direction.upper() == "SHORT"
    aligned = close < e10[i] < e20[i] < e50[i] if is_short else close > e10[i] > e20[i] > e50[i]
    slope_ok = e20[i] < e20[i - 3] and e50[i] <= e50[i - 3] if is_short else e20[i] > e20[i - 3] and e50[i] >= e50[i - 3]
    touched = highs[i] >= e10[i] * 0.998 if is_short else lows[i] <= e10[i] * 1.002
    recovered = close < e10[i] and close < closes[i - 1] if is_short else close > e10[i] and close > closes[i - 1]
    volatility_ok = close > 0 and atr_value / close >= params.min_atr_pct
    if not (aligned and slope_ok and touched and recovered and volatility_ok):
        return None
    stop = close + atr_value * params.stop_atr if is_short else close - atr_value * params.stop_atr
    take = close - atr_value * params.take_profit_atr if is_short else close + atr_value * params.take_profit_atr
    return {
        "symbol": symbol,
        "signal": "SHORT" if is_short else "LONG",
        "reason": "trend_pullback_continuation",
        "strategy": "trend_pullback",
        "last_price": close,
        "ema_fast": e20[i],
        "ema_slow": e50[i],
        "atr": atr_value,
        "stop": stop,
        "take_profit": take,
        "risk_pct": abs(close - stop) / close,
        "expected_profit_pct": abs(take - close) / close * 100,
        "entry_type": "trend_pullback",
        "entry_type_label": "趋势回踩",
        "protection_profile": {
            "stop_atr": params.stop_atr,
            "take_profit_atr": params.take_profit_atr,
            "max_hold_bars": params.max_hold_bars,
            "break_even_atr": 0.75,
            "trailing_trigger_atr": 1.05,
            "trailing_distance_atr": 0.65,
        },
        "trigger_price": e10[i],
        "distance_to_trigger_pct": 0.0,
        "candle_move_pct": abs(close - closes[i - 1]) / close * 100,
        "trend": True,
        "trigger": True,
        "volatility_ok": True,
        "multi_horizon_alignment": True,
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
    opportunity_by_symbol: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    limits = pipeline_limits(config, mode)
    manual = {symbol.upper() for symbol in config.get("stage1_symbols", [])}
    opportunity_by_symbol = opportunity_by_symbol or {}
    min_volume = float(config.get("min_24h_volume_usdt", 0))
    v3_enabled = opportunity_v3_active(config, mode.get("mode"))
    v3_market = config.get("_opportunity_v3_market_context") or {}
    v3_symbols = v3_market.get("symbols") or {}
    rows: list[dict[str, Any]] = []
    for index, symbol in enumerate(symbols):
        ticker = tickers.get(symbol, {})
        quote_volume = float(ticker.get("quoteVolume", 0) or 0)
        change_pct = float(ticker.get("priceChangePercent", 0) or 0)
        last_price = float(ticker.get("lastPrice", 0) or 0)
        v3_symbol = v3_symbols.get(symbol, {})
        if v3_enabled:
            volume_score = float(v3_symbol.get("volume_percentile") or 0) * 32.0
            directional_extreme = max(
                float(v3_symbol.get("long_strength_percentile") or 0.5),
                float(v3_symbol.get("short_strength_percentile") or 0.5),
            )
            move_score = max(0.0, (directional_extreme - 0.5) * 44.0)
        else:
            volume_score = min(max(math.log10(max(quote_volume, 1)) - 6.5, 0.0) * 12.0, 35.0)
            move_score = min(abs(change_pct) * 1.8, 28.0)
        if is_extreme_mode(mode.get("mode")) and config.get("extreme_v2_enabled", True) and not v3_enabled:
            move_score = min(abs(change_pct) * float(config.get("extreme_firecracker_move_score_weight", 2.4)), 40.0)
            volume_score = min(max(math.log10(max(quote_volume, 1)) - 6.5, 0.0) * float(config.get("extreme_firecracker_volume_score_weight", 12.0)), 38.0)
        direction_bias = min(abs(float(v3_symbol.get("residual_change_pct") or 0)) * 0.6, 8.0) if v3_enabled else 4.0 if change_pct > 0 else 2.0 if change_pct < 0 else 0.0
        manual_score = (4.0 if v3_enabled else 30.0) if symbol in manual else 0.0
        liquidity_penalty = 18.0 if quote_volume < min_volume else 0.0
        new_tail_boost = 0.0 if v3_enabled else max(0.0, 10.0 - index * 0.03)
        exhaustion_penalty = (
            8.0
            if v3_enabled and float(v3_symbol.get("absolute_move_percentile") or 0) >= 0.995
            else 0.0
        )
        score = volume_score + move_score + direction_bias + manual_score + new_tail_boost - liquidity_penalty - exhaustion_penalty
        reasons = []
        if symbol in manual:
            reasons.append("manual")
        if quote_volume >= min_volume:
            reasons.append("liquid")
        if abs(change_pct) >= 8:
            reasons.append("large_move")
        if quote_volume < min_volume:
            reasons.append("low_volume")
        if exhaustion_penalty:
            reasons.append("possible_exhaustion")
        firecracker_score = firecracker_opportunity_score(ticker, config, mode)
        if firecracker_score.get("is_firecracker"):
            reasons.append("firecracker")
            score += float(firecracker_score.get("score", 0)) * 0.25
        opportunity = opportunity_by_symbol.get(symbol)
        if opportunity:
            reasons.append("event_queue")
            score += float(opportunity.get("score") or 0) * float(config.get("opportunity_queue_score_weight", 0.35))
        rows.append(
            {
                "symbol": symbol,
                "score": round(score, 4),
                "quote_volume": quote_volume,
                "change_pct": change_pct,
                "last_price": last_price,
                "reasons": reasons,
                "firecracker": firecracker_score,
                "opportunity_event": opportunity or {},
                "v3_cross_section": v3_symbol,
            }
        )
    if is_extreme_mode(mode.get("mode")) and config.get("extreme_v2_enabled", True):
        rows.sort(key=lambda item: item["score"], reverse=True)
    else:
        rows.sort(key=lambda item: (item["symbol"] in manual, item["score"]), reverse=True)
    ranked = rows[: max(1, limits["coarse"])]
    rank_limit = max(1, limits["rank"])
    v44_active = bool(
        str(config.get("opportunity_v4_strategy_version") or "").lower().startswith(("v4.4", "v4.5"))
        and config.get("opportunity_v4_enabled", True)
    )
    if not v44_active or rank_limit < 3:
        symbols_out = [item["symbol"] for item in ranked[:rank_limit]]
        return symbols_out, ranked

    selected: list[str] = []

    def reserve(items: list[dict[str, Any]], count: int, route: str) -> None:
        for item in items:
            symbol = str(item.get("symbol") or "")
            if not symbol or symbol in selected:
                continue
            item.setdefault("reasons", []).append(route)
            selected.append(symbol)
            if sum(route in row.get("reasons", []) for row in ranked) >= count:
                break

    route_slots = max(1, min(rank_limit // 5, 6))
    reserve([item for item in ranked if item.get("opportunity_event")], route_slots, "v44_event_reserve")
    reserve(
        sorted(
            ranked,
            key=lambda item: float((item.get("v3_cross_section") or {}).get("long_strength_percentile") or 0),
            reverse=True,
        ),
        route_slots,
        "v44_long_reserve",
    )
    reserve(
        sorted(
            ranked,
            key=lambda item: float((item.get("v3_cross_section") or {}).get("short_strength_percentile") or 0),
            reverse=True,
        ),
        route_slots,
        "v44_short_reserve",
    )
    for item in ranked:
        symbol = str(item.get("symbol") or "")
        if symbol and symbol not in selected:
            selected.append(symbol)
        if len(selected) >= rank_limit:
            break
    symbols_out = selected[:rank_limit]
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


def firecracker_opportunity_score(
    ticker: dict[str, Any],
    config: dict[str, Any],
    mode: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not config.get("extreme_firecracker_enabled", True):
        return {"enabled": False, "score": 0.0, "is_firecracker": False, "reasons": ["disabled"]}
    if not is_extreme_mode((mode or {}).get("mode")) or not config.get("extreme_v2_enabled", True):
        return {"enabled": False, "score": 0.0, "is_firecracker": False, "reasons": ["not_extreme_v2"]}
    quote_volume = float(ticker.get("quoteVolume", 0) or 0)
    change_pct = float(ticker.get("priceChangePercent", 0) or 0)
    trade_count = float(ticker.get("count", 0) or 0)
    min_volume = float(config.get("extreme_firecracker_min_quote_volume_usdt", 30_000_000))
    adaptive = adaptive_thresholds([], config)
    min_move = float(adaptive.get("firecracker_move_min_pct", config.get("extreme_firecracker_min_abs_change_pct", 8.0)))
    volume_score = min(max(math.log10(max(quote_volume, 1)) - 6.5, 0.0) * 12.0, 35.0)
    move_score = min(abs(change_pct) * 2.4, 42.0)
    activity_score = min(math.log10(max(trade_count, 1)) * 4.0, 18.0)
    score = volume_score + move_score + activity_score
    reasons: list[str] = []
    if quote_volume >= min_volume:
        reasons.append("volume_ok")
    else:
        reasons.append("volume_low")
        score -= 15
    if abs(change_pct) >= min_move:
        reasons.append("large_move")
    if quote_volume >= min_volume * 4:
        reasons.append("high_liquidity")
        score += 5
    score = max(0.0, min(score, 100.0))
    is_firecracker = score >= float(config.get("extreme_firecracker_min_score", 55.0)) and quote_volume >= min_volume and abs(change_pct) >= min_move
    return {
        "enabled": True,
        "score": round(score, 2),
        "is_firecracker": is_firecracker,
        "quote_volume": quote_volume,
        "change_pct": change_pct,
        "trade_count": trade_count,
        "reasons": reasons,
        "adaptive_thresholds": adaptive,
    }


def squeeze_signal(bars: list[list[Any]], config: dict[str, Any]) -> dict[str, Any]:
    if not config.get("extreme_squeeze_enabled", True):
        return {"enabled": False, "active": False, "released": False, "score": 0.0}
    lookback = int(config.get("extreme_squeeze_lookback", 20))
    if len(bars) < lookback + 5:
        return {"enabled": True, "active": False, "released": False, "score": 0.0, "reason": "not_enough_data"}
    closes = [float(row[4]) for row in bars]
    recent = closes[-lookback:]
    mean = sum(recent) / len(recent)
    variance = sum((value - mean) ** 2 for value in recent) / len(recent)
    stdev = math.sqrt(variance)
    atr_values = atr(bars, 20)
    atr_value = float(atr_values[-1] or 0)
    width_pct = (stdev * 4) / mean * 100 if mean else 0.0
    atr_width_pct = (atr_value * 3) / mean * 100 if mean else 0.0
    compression = width_pct < atr_width_pct and width_pct <= 3.0
    last = closes[-1]
    previous_high = max(float(row[2]) for row in bars[-lookback - 1:-1])
    previous_low = min(float(row[3]) for row in bars[-lookback - 1:-1])
    released_long = compression and last > previous_high
    released_short = compression and last < previous_low
    score = float(config.get("extreme_squeeze_bonus", 8.0)) if released_long or released_short else (float(config.get("extreme_squeeze_bonus", 8.0)) * 0.5 if compression else 0.0)
    return {
        "enabled": True,
        "active": compression,
        "released": released_long or released_short,
        "direction": "LONG" if released_long else "SHORT" if released_short else None,
        "score": round(score, 2),
        "width_pct": round(width_pct, 4),
        "atr_width_pct": round(atr_width_pct, 4),
    }


def derivative_confirmation(
    client: BinanceFuturesClient,
    symbol: str,
    direction: str,
    ticker: dict[str, Any],
    funding: dict[str, Any] | None,
    config: dict[str, Any],
    enabled: bool = True,
) -> dict[str, Any]:
    if not enabled or not config.get("extreme_derivatives_enabled", True):
        return {"enabled": False, "confirmed": False, "score_delta": 0.0, "risk_multiplier": 1.0, "reasons": ["disabled"]}
    reasons: list[str] = []
    oi_growth_pct = 0.0
    try:
        hist = client.open_interest_hist(symbol, str(config.get("extreme_sprint_interval", "5m")), 12)
        if len(hist) >= 2:
            first = float(hist[0].get("sumOpenInterest", hist[0].get("sumOpenInterestValue", 0)) or 0)
            last = float(hist[-1].get("sumOpenInterest", hist[-1].get("sumOpenInterestValue", 0)) or 0)
            if first > 0:
                oi_growth_pct = (last - first) / first * 100
    except Exception as exc:
        reasons.append(f"oi_failed:{exc}")
    change_pct = float(ticker.get("priceChangePercent", 0) or 0)
    funding_rate_pct = float((funding or {}).get("lastFundingRate", 0) or 0) * 100
    mark_price = float((funding or {}).get("markPrice", 0) or 0)
    index_price = float((funding or {}).get("indexPrice", 0) or 0)
    basis_pct = (mark_price - index_price) / index_price * 100 if index_price > 0 else 0.0
    min_growth = float(config.get("extreme_oi_min_growth_pct", 1.5))
    strong_growth = float(config.get("extreme_oi_strong_growth_pct", 4.0))
    crowded = abs(funding_rate_pct) >= float(config.get("extreme_funding_abs_crowded_pct", 0.05))
    direction_aligned = change_pct > 0 if direction == "LONG" else change_pct < 0
    confirmed = oi_growth_pct >= min_growth and direction_aligned
    score_delta = 0.0
    risk_multiplier = 1.0
    if confirmed:
        score_delta += float(config.get("extreme_derivative_confirm_bonus", 10.0))
        reasons.append("oi_price_confirmed")
        if oi_growth_pct >= strong_growth:
            score_delta += float(config.get("extreme_derivative_confirm_bonus", 10.0)) * 0.5
            risk_multiplier *= 1.12
            reasons.append("strong_oi")
    elif oi_growth_pct <= 0 and abs(change_pct) >= float(config.get("extreme_firecracker_min_abs_change_pct", 8.0)):
        penalty = float(config.get("extreme_derivative_divergence_penalty", 12.0))
        score_delta -= penalty
        risk_multiplier *= 0.65
        reasons.append("price_without_oi")
    if crowded:
        reasons.append("funding_crowded")
        if (direction == "LONG" and funding_rate_pct > 0) or (direction == "SHORT" and funding_rate_pct < 0):
            score_delta -= float(config.get("extreme_derivative_divergence_penalty", 12.0)) * 0.5
            risk_multiplier *= 0.75
        else:
            score_delta += 3.0
    basis_aligned = (direction == "LONG" and basis_pct >= 0) or (direction == "SHORT" and basis_pct <= 0)
    if abs(basis_pct) >= float(config.get("opportunity_v3_basis_confirm_pct", 0.02)):
        if basis_aligned:
            score_delta += float(config.get("opportunity_v3_basis_confirm_bonus", 2.0))
            reasons.append("basis_aligned")
        elif abs(basis_pct) >= float(config.get("opportunity_v3_basis_divergence_pct", 0.08)):
            score_delta -= float(config.get("opportunity_v3_basis_divergence_penalty", 3.0))
            risk_multiplier *= 0.9
            reasons.append("basis_divergence")
    return {
        "enabled": True,
        "confirmed": confirmed,
        "score_delta": round(score_delta, 2),
        "risk_multiplier": round(max(0.0, min(risk_multiplier, 1.25)), 4),
        "oi_growth_pct": round(oi_growth_pct, 4),
        "funding_rate_pct": round(funding_rate_pct, 5),
        "basis_pct": round(basis_pct, 5),
        "basis_aligned": basis_aligned,
        "reasons": reasons,
    }


def spot_proxy_confirmation(
    bars: list[list[Any]],
    direction: str,
    signal: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    if not config.get("extreme_spot_proxy_enabled", True):
        return {"enabled": False, "confirmed": False, "score_delta": 0.0, "risk_multiplier": 1.0}
    spike = _volume_spike_ratio(bars)
    candle_move = float(signal.get("candle_move_pct") or 0)
    confirmed = spike >= float(config.get("extreme_spot_proxy_min_volume_spike", 1.1)) and candle_move >= 0.15
    if confirmed:
        return {
            "enabled": True,
            "confirmed": True,
            "score_delta": float(config.get("extreme_spot_proxy_bonus", 6.0)),
            "risk_multiplier": 1.05,
            "volume_spike": round(spike, 4),
            "candle_move_pct": round(candle_move, 4),
            "reason": "volume_price_confirmed",
        }
    return {
        "enabled": True,
        "confirmed": False,
        "score_delta": -float(config.get("extreme_spot_proxy_penalty", 8.0)) if candle_move >= 0.3 else 0.0,
        "risk_multiplier": 0.82 if candle_move >= 0.3 else 1.0,
        "volume_spike": round(spike, 4),
        "candle_move_pct": round(candle_move, 4),
        "reason": "weak_volume_confirmation",
    }


def _depth_metrics_from_book(depth: dict[str, Any]) -> dict[str, float | bool | str]:
    try:
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
            "micro_price": float(depth.get("micro_price") or mid),
            "microprice_edge_bps": float(depth.get("microprice_edge_bps") or 0),
            "trade_flow_notional": float(depth.get("trade_flow_notional") or 0),
            "trade_flow_imbalance": float(depth.get("trade_flow_imbalance") or 0),
        }
    except Exception as exc:
        return {"available": False, "spread_pct": 999.0, "depth_notional": 0.0, "reason": str(exc)}


def _depth_metrics(client: BinanceFuturesClient, symbol: str) -> dict[str, float | bool | str]:
    try:
        return _depth_metrics_from_book(client.depth(symbol, limit=5))
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
    "extreme_sprint": {
        "volume": 7,
        "volume_spike": 22,
        "volatility": 20,
        "spread_depth": 17,
        "backtest_3d": 16,
        "backtest_5d": 7,
        "backtest_10d": 0,
        "trend": 11,
    },
    "yolo_scalp": {
        "volume": 6,
        "volume_spike": 26,
        "volatility": 22,
        "spread_depth": 20,
        "backtest_3d": 10,
        "backtest_5d": 4,
        "backtest_10d": 0,
        "trend": 12,
    },
}


ATR_IDEAL_RANGES: dict[str, tuple[float, float, float]] = {
    "conservative": (0.4, 2.5, 4.0),
    "balanced": (0.5, 3.5, 5.0),
    "attack": (0.8, 4.5, 7.0),
    "tournament": (1.0, 5.5, 8.0),
    "extreme_sprint": (1.2, 8.0, 11.0),
    "yolo_scalp": (1.5, 10.0, 14.0),
}


def _mode_name(config: dict[str, Any], mode: dict[str, Any] | None = None) -> str:
    name = str((mode or {}).get("mode") or config.get("growth_mode") or "balanced").lower()
    return name if name in QUALITY_WEIGHTS else "balanced"


def _score_volatility_for_mode(atr_pct: float, mode_name: str, config: dict[str, Any]) -> float:
    if atr_pct <= 0:
        return 0.0
    if mode_name in {"tournament_sprint", "extreme_sprint", "yolo_scalp"}:
        ideal_min = float(config.get("sprint_atr_ideal_min_pct", 1.2))
        ideal_max = float(config.get("sprint_atr_ideal_max_pct", 10.0 if mode_name == "yolo_scalp" else 8.0 if mode_name == "extreme_sprint" else 7.0))
        high = float(config.get("sprint_atr_high_pct", 14.0 if mode_name == "yolo_scalp" else 11.0 if mode_name == "extreme_sprint" else 10.0))
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
    if mode_name in {"tournament_sprint", "extreme_sprint", "yolo_scalp"}:
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
    if mode_name in {"tournament_sprint", "extreme_sprint", "yolo_scalp"} and sample_low and not sample_exempt:
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
        mode_name in {"tournament_sprint", "extreme_sprint", "yolo_scalp"}
        and spike >= float(config.get("sprint_sample_penalty_exempt_spike", 2.5))
        and bool(signal.get("trend") is True or signal.get("signal") in {"LONG", "SHORT"})
        and float(depth.get("depth_notional", 0)) >= float(config.get("min_depth_notional_usdt", 20_000))
    )
    if sample_low and not sample_exempt:
        false_breakout_penalty += (
            float(config.get("sprint_sample_penalty", 1.0))
            if mode_name in {"tournament_sprint", "extreme_sprint", "yolo_scalp"}
            else {"conservative": 10.0, "balanced": 8.0, "attack": 5.0, "tournament": 3.0}.get(mode_name, 8.0)
        )
    if float(primary.get("win_rate", 0)) < 35 and int(primary.get("trades", 0)) >= 5:
        false_breakout_penalty += 8.0
    high_atr_penalty = 0.0
    if mode_name != "tournament_sprint" and atr_pct > 5.5:
        high_atr_penalty = 6.0
    elif mode_name in {"tournament_sprint", "extreme_sprint", "yolo_scalp"} and atr_pct > float(config.get("sprint_atr_high_pct", 14.0 if mode_name == "yolo_scalp" else 10.0)):
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
    trade_score = float(config.get("yolo_symbol_trade_score", 60.0) if mode_name == "yolo_scalp" else config.get("sprint_symbol_trade_score", 68.0) if mode_name in {"tournament_sprint", "extreme_sprint"} else config.get("symbol_trade_score", 75.0))
    small_score = float(config.get("yolo_symbol_small_trade_score", 48.0) if mode_name == "yolo_scalp" else config.get("sprint_symbol_small_trade_score", 55.0) if mode_name in {"tournament_sprint", "extreme_sprint"} else config.get("symbol_small_trade_score", 65.0))
    hot_score = float(config.get("sprint_symbol_hot_observe_score", 45.0))
    observe_score = float(config.get("symbol_observe_score", 50.0))
    market_passed = (
        float(depth.get("spread_pct", 999)) <= float(config.get("max_spread_pct", 0.08))
        and float(depth.get("depth_notional", 0)) >= float(config.get("min_depth_notional_usdt", 20_000))
    )
    hot_observe = (
        mode_name in {"tournament_sprint", "extreme_sprint", "yolo_scalp"}
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
    if mode.get("mode") not in {"tournament", "tournament_sprint", "yolo_scalp"}:
        return False
    if quality.get("pool") != "observe":
        return False
    if signal.get("signal") not in {"LONG", "SHORT"}:
        return False
    if mode.get("mode") in {"tournament_sprint", "yolo_scalp"}:
        prefix = "yolo_scalp" if mode.get("mode") == "yolo_scalp" else "tournament_sprint"
        if candidate_score < float(config.get(f"{prefix}_standard_min_score", config.get("standard_min_score", 85.0))):
            return False
        quality_floor = float(config.get("yolo_observe_breakout_min_quality", 58.0) if prefix == "yolo_scalp" else config.get("observe_breakout_min_quality", 78.0) - 5.0)
        if float(quality.get("score", 0)) < quality_floor:
            return False
        if cost_ratio < float(config.get(f"{prefix}_min_expected_profit_cost_ratio", 1.35)):
            return False
        if float(recent.get("profit_factor", 0)) < float(config.get(f"{prefix}_long_min_profit_factor", 0.75 if prefix == "yolo_scalp" else 0.85)):
            return False
        if float(recent.get("net_pct", 0)) < float(config.get(f"{prefix}_long_min_net_pct", -8.0 if prefix == "yolo_scalp" else -3.0)):
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


def weak_quality_probe_allows_entry(
    candidate_score: float,
    quality: dict[str, Any],
    recent: dict[str, Any],
    signal: dict[str, Any],
    cost_ratio: float,
    depth: dict[str, Any],
    config: dict[str, Any],
    mode: dict[str, Any],
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not config.get("weak_quality_probe_enabled", True):
        return False, ["弱质量试探未开启"]
    if not is_extreme_mode(mode.get("mode")):
        return False, ["仅极限冲刺启用"]
    if quality.get("pool") not in {"observe", "observe_hot"}:
        return False, [f"质量池不是观察池({quality.get('pool')})"]
    if signal.get("signal") not in {"LONG", "SHORT"}:
        return False, ["没有真实突破信号"]
    checks = [
        (candidate_score >= float(config.get("weak_quality_probe_min_candidate_score", 95.0)), "候选分不足"),
        (float(quality.get("score", 0)) >= float(config.get("weak_quality_probe_min_quality_score", 58.0)), "质量分不足"),
        (cost_ratio >= float(config.get("weak_quality_probe_min_cost_ratio", 12.0)), "成本比不足"),
        (float(recent.get("profit_factor", 0)) >= float(config.get("weak_quality_probe_min_profit_factor", 0.55)), "PF过低"),
        (float(recent.get("net_pct", 0)) >= float(config.get("weak_quality_probe_min_net_pct", -8.0)), "净收益过低"),
        (float(depth.get("spread_pct", 999)) <= float(config.get("weak_quality_probe_max_spread_pct", 0.12)), "点差过大"),
        (float(depth.get("depth_notional", 0)) >= float(config.get("weak_quality_probe_min_depth_notional_usdt", 300.0)), "盘口深度不足"),
    ]
    for passed, reason in checks:
        if not passed:
            reasons.append(reason)
    return not reasons, reasons


def weak_quality_probe_risk_adjustment(
    quality: dict[str, Any],
    recent: dict[str, Any],
    depth: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    quality_score = float(quality.get("score", 0) or 0)
    if quality_score >= float(config.get("weak_quality_probe_high_quality_score", 72.0)):
        multiplier = float(config.get("weak_quality_probe_high_multiplier", 0.35))
        tier = "高观察质量"
    elif quality_score >= float(config.get("weak_quality_probe_mid_quality_score", 65.0)):
        multiplier = float(config.get("weak_quality_probe_mid_multiplier", 0.25))
        tier = "中观察质量"
    else:
        multiplier = float(config.get("weak_quality_probe_base_multiplier", 0.18))
        tier = "弱观察质量"
    reasons = [f"{tier} {quality_score:.2f}，基础倍率 {multiplier:.2f}x"]
    if float(recent.get("profit_factor", 0)) < 0.8:
        pf_mult = float(config.get("weak_quality_probe_low_pf_multiplier", 0.7))
        multiplier *= pf_mult
        reasons.append(f"PF偏低折扣 {pf_mult:.2f}x")
    if float(recent.get("net_pct", 0)) < 0:
        net_mult = float(config.get("weak_quality_probe_negative_net_multiplier", 0.8))
        multiplier *= net_mult
        reasons.append(f"回测净收益为负折扣 {net_mult:.2f}x")
    min_depth = float(config.get("weak_quality_probe_min_depth_notional_usdt", 300.0))
    if min_depth > 0 and float(depth.get("depth_notional", 0)) < min_depth * 2:
        depth_mult = float(config.get("weak_quality_probe_weak_depth_multiplier", 0.7))
        multiplier *= depth_mult
        reasons.append(f"盘口深度偏弱折扣 {depth_mult:.2f}x")
    return {"type": "weak_quality_probe", "multiplier": round(multiplier, 6), "reasons": reasons}


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


def _finalize_candidate(
    candidate: dict[str, Any],
    *,
    config: dict[str, Any],
    mode: dict[str, Any],
    exchange_filters: ExchangeFilters | None,
    equity: float | None,
) -> dict[str, Any]:
    v44_full_bet = s0_full_bet_profile_active(config)
    if not v44_full_bet:
        candidate = apply_live_credit_to_candidate(candidate, config)
        candidate = apply_live_reaction_to_candidate(candidate, config)
        candidate = apply_strategy_evidence_to_candidate(candidate, config)
    viability_risk_pct = float(candidate.get("risk_pct") or 0)
    if v44_full_bet:
        viability_risk_pct = max(
            viability_risk_pct,
            float(config.get("opportunity_v44_max_risk_pct", 13.0)),
        )
    if config.get("effective_position_sizing_enabled", True):
        viability_risk_pct = float(
            effective_position_risk(
                candidate_risk_pct=viability_risk_pct,
                candidate=candidate,
                guard={"risk_multiplier": 1.0},
                target={"effective_risk_multiplier": 1.0},
                config=config,
                mode=str(mode["mode"]),
            )["final_risk_pct"]
        )
    signal = candidate.get("signal") or {}
    viability = execution_viability(
        exchange_filters,
        str(candidate.get("symbol") or ""),
        equity,
        viability_risk_pct,
        float(signal.get("last_price") or 0),
        float(signal.get("stop") or 0),
        float(mode["margin_pct"]) / 100 * float(equity or 0) * float(mode["leverage"]),
    )
    viability["effective_risk_pct"] = viability_risk_pct
    candidate["execution_filter"] = viability
    if candidate.get("passed") and viability.get("enabled") and not viability.get("executable"):
        candidate["passed"] = False
        candidate["reason"] = "min_order_not_executable"
        candidate["decision_reason"] = (
            f"低于币安最小下单量，当前约 {viability.get('notional')}U，"
            f"最低 {viability.get('min_notional')}U，跳过避免启动后失败"
        )
    return candidate


def _apply_v4_live_selection(
    candidate: dict[str, Any],
    config: dict[str, Any],
    mode: dict[str, Any],
) -> dict[str, Any]:
    """Make V4 the live selector; legacy features remain internal compatibility inputs."""
    if not config.get("opportunity_v4_enabled", True) or not config.get("opportunity_v4_live_enabled", False):
        return candidate
    result = dict(candidate)
    v4 = dict(candidate.get("opportunity_v4") or {})
    if not v4:
        result["passed"] = False
        result["reason"] = "opportunity_v4_no_trigger"
        result["decision_reason"] = "V4 未识别到可执行的实时趋势触发"
        return result

    admitted = bool(v4.get("admitted"))
    risk_multiplier = float(v4.get("risk_multiplier") or 0.0)
    base_risk = float(candidate.get("base_risk_pct") or mode.get("risk_pct") or 0.0)
    version = str(v4.get("strategy_version") or config.get("opportunity_v4_strategy_version") or "v4.3.2")
    version_label = version.upper()
    full_bet = bool(version.lower().startswith(("v4.4", "v4.5")) and v4.get("full_bet_admitted"))
    signal = dict(candidate.get("signal") or {})
    if v4.get("protection_profile"):
        signal["protection_profile"] = dict(v4["protection_profile"])
    result.update(
        {
            "strategy": "opportunity_v44_full_bet" if full_bet else "opportunity_v432_continuous_roll",
            "strategy_family": V4_STRATEGY_FAMILY,
            "strategy_version": version,
            "strategy_role": "active",
            "strategy_generation": version,
            "signal": signal,
            "score": float(v4.get("score") or 0),
            "passed": admitted,
            "reason": "passed" if admitted else "opportunity_v4_not_ready",
            "decision_reason": str(v4.get("reason") or "V4 候选未达到实盘准入"),
            "risk_pct": (
                float(mode.get("risk_pct") or base_risk)
                if full_bet
                else min(float(mode.get("risk_pct") or base_risk), base_risk * risk_multiplier)
            ),
            # risk_pct already contains the V4 admission multiplier. Keep the legacy
            # sizing multipliers neutral so execution cannot apply it a second time.
            "quality_risk_multiplier": 1.0,
            "v4_risk_multiplier": risk_multiplier,
            "quality_risk_reasons": [
                f"{version_label} 已验证核心准入"
                if v4.get("validated")
                else f"{version_label} 同状态核心准入"
                if v4.get("provisional")
                else f"{version_label} 核心限次试运行"
                if v4.get("bootstrap_admitted")
                else f"{version_label} 顺势受限探索"
                if v4.get("exploration_admitted")
                else f"{version_label} 仅影子观察"
            ],
            "symbol_quality": {
                "engine": "opportunity_v4",
                "score": float(v4.get("score") or 0),
                "allowed": admitted,
                "pool": "trade" if admitted else "observe",
                "tier": (
                    f"{version_label}-CORE"
                    if v4.get("validated")
                    else f"{version_label}-CORE-LIMITED"
                    if v4.get("provisional")
                    else f"{version_label}-CORE-CANARY"
                    if v4.get("bootstrap_admitted")
                    else f"{version_label}-EXPLORE"
                    if v4.get("exploration_admitted")
                    else f"{version_label}-SHADOW"
                ),
                "quality_risk_multiplier": risk_multiplier,
                "quality_risk_reasons": [str(v4.get("reason") or f"{version_label} 独立排序")],
                "continuous_position_confidence": v4.get("position_confidence") or {},
                "simulation": {
                    "passed": None,
                    "diagnostic": f"{version_label} 按实盘准入与研究影子隔离事件级证据",
                },
            },
            "symbol_pool": "trade" if admitted else "observe",
            "risk_adjustment": {
                "type": "opportunity_v4_rank",
                "multiplier": 1.0,
                "v4_admission_multiplier": round(risk_multiplier, 6),
                "rank_percentile": v4.get("rank_percentile"),
                "evidence_status": v4.get("evidence_status"),
                "admission_lane": v4.get("admission_lane"),
                "position_confidence": v4.get("position_confidence") or {},
            },
        }
    )
    if full_bet:
        result["quality_risk_reasons"] = [f"{version.upper()} 当前轮相对排名与五项确认通过"]
        result["symbol_quality"] = {
            **(result.get("symbol_quality") or {}),
            "tier": f"{version.upper()}-FULL-BET",
            "quality_risk_multiplier": 1.0,
            "quality_risk_reasons": [str(v4.get("reason") or f"{version.upper()} 独立排序")],
            "continuous_position_confidence": v4.get("position_confidence") or {},
            "simulation": {
                "passed": None,
                "diagnostic": f"只使用 {version.upper()} 当前版本影子和实盘证据，旧版本信用不参与准入",
            },
        }
        result["risk_adjustment"] = {
            **(result.get("risk_adjustment") or {}),
            "type": "v44_full_bet_dynamic_risk",
            "multiplier": 1.0,
            "v4_admission_multiplier": 1.0,
            "admission_lane": "full_bet",
        }
    return result


def scan_growth_candidates(
    client: BinanceFuturesClient,
    config: dict[str, Any],
    account_summary: dict[str, Any],
    symbols_override: list[str] | None = None,
    fast_lane: bool = False,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    equity = account_summary.get("equity")
    mode = mode_config(config, equity)
    limits = pipeline_limits(config, mode)
    symbols = (
        list(dict.fromkeys(str(symbol).upper() for symbol in symbols_override if str(symbol).upper().endswith("USDT")))
        if symbols_override
        else discover_coin_symbols(client, config)
    )
    opportunity_events = read_opportunities(
        max_age_seconds=int(config.get("opportunity_queue_ttl_seconds", 240)),
        limit=int(config.get("opportunity_queue_scan_limit", 50)),
    ) if config.get("opportunity_queue_enabled", True) else []
    trigger_events = stream_triggers(
        max_age_seconds=int(config.get("websocket_trigger_max_age_seconds", 180)),
        limit=int(config.get("websocket_trigger_scan_limit", 40)),
    ) if config.get("websocket_trigger_enabled", True) else []
    if not fast_lane:
        for event in opportunity_events:
            event_symbol = str(event.get("symbol") or "").upper()
            if event_symbol and event_symbol not in symbols:
                symbols.append(event_symbol)
        for event in trigger_events:
            trigger_symbol = str(event.get("symbol") or "").upper()
            if trigger_symbol and trigger_symbol not in symbols:
                symbols.append(trigger_symbol)
    excluded_symbols = {str(symbol).upper() for symbol in config.get("_excluded_scan_symbols", []) or []}
    if excluded_symbols:
        symbols = [symbol for symbol in symbols if symbol not in excluded_symbols]
    candidates = []
    recalled_symbols = list(symbols)
    try:
        all_ticker_rows = client.ticker_24h()
    except TypeError:
        # Compatibility fallback for clients that require an explicit symbol list.
        all_ticker_rows = client.ticker_24h(recalled_symbols)
    recalled_set = set(recalled_symbols)
    tickers = {item["symbol"]: item for item in all_ticker_rows if item.get("symbol") in recalled_set}
    market_profile = build_market_profile(tickers)
    v3_market_context = build_market_context(all_ticker_rows, config)
    config = {
        **config,
        "_adaptive_market_profile": market_profile,
        "_opportunity_v3_market_context": v3_market_context,
    }
    v3_enabled = opportunity_v3_active(config, mode.get("mode"))
    try:
        funding_by_symbol = {item["symbol"]: item for item in client.premium_index(recalled_symbols)}
    except Exception:
        funding_by_symbol = {}
    opportunity_by_symbol = {str(event.get("symbol") or "").upper(): event for event in opportunity_events}
    trigger_by_symbol = {str(event.get("symbol") or "").upper(): event for event in trigger_events}
    ranked_symbols, coarse_rows = _coarse_rank_symbols(recalled_symbols, tickers, config, mode, opportunity_by_symbol)
    opportunity_symbols = [str(event.get("symbol") or "").upper() for event in opportunity_events]
    trigger_symbols = [str(event.get("symbol") or "").upper() for event in trigger_events]
    ranked_symbols = sorted(
        ranked_symbols,
        key=lambda value: (
            value in opportunity_symbols,
            value in trigger_symbols,
            -(opportunity_symbols.index(value) if value in opportunity_symbols else 999999),
            -(trigger_symbols.index(value) if value in trigger_symbols else 999999),
        ),
        reverse=True,
    )
    v31_bars_by_symbol = _v31_medium_bars(client, ranked_symbols, config) if v3_enabled else {}
    v31_medium_context = build_v31_medium_context(v31_bars_by_symbol) if v31_bars_by_symbol else {}
    fee_pct = StrategyParams().taker_fee * 2 * 100
    slippage_pct = float(config.get("estimated_slippage_pct", 0.04))
    cost_pct = fee_pct + slippage_pct
    observed_cost_pct = max(cost_pct, observed_round_trip_cost_pct(config))
    quality_days = sorted({
        int(day)
        for day in config.get("quality_backtest_days", [3, 5])
        if int(day) > 0
    } | {int(mode["recent_days"])})
    max_depth_checks = min(
        int(config.get("fast_lane_depth_checks", 3)) if fast_lane else int(config.get("depth_check_top_symbols", 8)),
        limits["auction"],
    )
    degrade_seconds = float(config.get("fast_lane_budget_seconds", 5.0) if fast_lane else config.get("scan_degrade_seconds", 18))
    min_rank_symbols = min(
        int(config.get("fast_lane_max_symbols", 3)) if fast_lane else int(config.get("scan_min_rank_symbols", 8)),
        len(ranked_symbols),
    )
    if fast_lane:
        ranked_symbols = ranked_symbols[:max(1, int(config.get("fast_lane_max_symbols", 3)))]
    depth_checks = 0
    depth_by_symbol: dict[str, dict[str, Any]] = {}
    live_losses_by_direction: dict[str, dict[str, Any]] = {}
    processed_symbols: list[str] = []
    derivative_checks = 0
    exchange_filters = None
    if config.get("min_order_filter_enabled", False):
        try:
            exchange_filters = ExchangeFilters(client.exchange_info())
        except Exception:
            exchange_filters = None

    for symbol in ranked_symbols:
        if len(processed_symbols) >= min_rank_symbols and time.perf_counter() - started_at >= degrade_seconds:
            break
        processed_symbols.append(symbol)
        try:
            bars = client.klines_history(symbol, mode["interval"], max(quality_days))
            directions = ["LONG", "SHORT"] if config.get("allow_short", False) else ["LONG"]
            for direction in directions:
                is_sprint = mode["mode"] in {"tournament_sprint", "extreme_sprint", "yolo_scalp"}
                fast_prefix = "yolo_scalp" if mode["mode"] == "yolo_scalp" else "extreme_sprint" if mode["mode"] == "extreme_sprint" else "tournament_sprint"
                signal_params = strategy_params_for_mode(config, mode, "standard")
                signal = (
                    build_v3_signal(symbol, bars, direction, config)
                    if v3_enabled
                    else latest_strategy_signal(symbol, bars, mode["strategy"], params=signal_params, direction=direction)
                )
                if signal.get("signal") == "WAIT" and mode["mode"] == "extreme_sprint" and not v3_enabled:
                    pullback = trend_pullback_signal(symbol, bars, direction, signal_params)
                    if pullback is not None:
                        signal = pullback
                if v3_enabled and not config.get("opportunity_v3_realtime_backtest_enabled", False):
                    backtests = {
                        day: {
                            "symbol": symbol,
                            "direction": direction,
                            "days": day,
                            "trades": 0,
                            "wins": 0,
                            "win_rate": 0.0,
                            "net_pct": 0.0,
                            "profit_factor": 0.0,
                            "diagnostic": "offline_v3_validation",
                        }
                        for day in quality_days
                    }
                else:
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
                    min_trades = int(config.get(f"{fast_prefix}_short_min_recent_trades", config.get("tournament_sprint_short_min_recent_trades", 3)) if is_sprint else config.get("short_min_recent_trades", 5))
                    min_pf = float(config.get(f"{fast_prefix}_short_min_profit_factor", config.get("tournament_sprint_short_min_profit_factor", 1.05)) if is_sprint else config.get("short_min_profit_factor", 1.3))
                    min_net_pct = float(config.get(f"{fast_prefix}_short_min_net_pct", config.get("tournament_sprint_short_min_net_pct", -2.0)) if is_sprint else config.get("short_min_net_pct", 1.0))
                    risk_pct = float(mode["risk_pct"]) * float(config.get("short_risk_multiplier", 0.5))
                else:
                    min_trades = int(mode["min_trades"])
                    min_pf = float(config.get(f"{fast_prefix}_long_min_profit_factor", config.get("tournament_sprint_long_min_profit_factor", mode["min_pf"])) if is_sprint else mode["min_pf"])
                    min_net_pct = float(config.get(f"{fast_prefix}_long_min_net_pct", config.get("tournament_sprint_long_min_net_pct", -3.0)) if is_sprint else 0.0)
                    risk_pct = float(mode["risk_pct"])
                if direction not in live_losses_by_direction and not v3_enabled:
                    live_losses_by_direction[direction] = consecutive_live_losses(client, processed_symbols or ranked_symbols, direction, config)

                current_score = _current_signal_score(signal, direction, cost_ratio, recent)
                should_check_depth = (
                    max_depth_checks > 0
                    and depth_checks < max_depth_checks
                    and (
                        signal.get("signal") == direction
                        or current_score >= float(config.get("depth_check_min_current_score", 38.0))
                        or (
                            mode["mode"] == "yolo_scalp"
                            and config.get("yolo_scalp_orderbook_engine_enabled", True)
                            and symbol in (opportunity_by_symbol.keys() | trigger_by_symbol.keys())
                        )
                    )
                )
                if v3_enabled and signal.get("signal") == direction and symbol not in depth_by_symbol:
                    streamed_depth = stream_depth(symbol, max_age_seconds=int(config.get("opportunity_v3_stream_depth_max_age_seconds", 8)))
                    if streamed_depth:
                        depth_by_symbol[symbol] = _depth_metrics_from_book(streamed_depth)
                if should_check_depth and symbol not in depth_by_symbol:
                    depth_by_symbol[symbol] = _depth_metrics(client, symbol)
                    depth_checks += 1
                depth = depth_by_symbol.get(symbol, _unchecked_depth_metrics())
                should_check_live = not v3_enabled and (
                    signal.get("signal") == direction
                    or current_score >= float(config.get("live_performance_check_min_score", 70.0))
                )
                live_perf = (
                    live_performance_summary(client, symbol, direction, config)
                    if should_check_live
                    else {"enabled": False, "reason": "candidate_score_low"}
                )
                firecracker = firecracker_opportunity_score(ticker, config, mode)
                squeeze = squeeze_signal(bars, config) if is_extreme_mode(mode["mode"]) else {"enabled": False, "score": 0.0}
                check_derivatives = (
                    is_extreme_mode(mode["mode"])
                    and (v3_enabled or config.get("extreme_v2_enabled", True))
                    and derivative_checks < int(config.get("extreme_oi_check_top_symbols", 12))
                    and (
                        firecracker.get("is_firecracker")
                        or signal.get("signal") == direction
                        or current_score >= float(config.get("depth_check_min_current_score", 38.0))
                    )
                )
                derivatives = derivative_confirmation(
                    client,
                    symbol,
                    direction,
                    ticker,
                    funding_by_symbol.get(symbol),
                    config,
                    enabled=check_derivatives,
                )
                if check_derivatives:
                    derivative_checks += 1
                if v3_enabled:
                    event = opportunity_by_symbol.get(symbol) or trigger_by_symbol.get(symbol)
                    v4_enabled = bool(config.get("opportunity_v4_enabled", True))
                    structure = build_market_structure_features(
                        symbol=symbol,
                        direction=direction,
                        signal=signal,
                        market_context=v3_market_context,
                        medium_context=v31_medium_context,
                    )
                    if not v4_enabled:
                        opportunity = score_v3_opportunity(
                            symbol=symbol,
                            direction=direction,
                            signal=signal,
                            ticker=ticker,
                            market_context=v3_market_context,
                            medium_context=v31_medium_context,
                            depth=depth,
                            derivatives=derivatives,
                            event=event,
                            cost_pct=observed_cost_pct,
                            config=config,
                        )
                        opportunity = calibrate_v3_opportunity(opportunity, signal, direction, config)
                        challenger = build_v33_challenger(
                            symbol=symbol,
                            direction=direction,
                            signal=signal,
                            opportunity=opportunity,
                            medium_context=v31_medium_context,
                            config=config,
                        )
                    else:
                        expected = float(signal.get("expected_profit_pct") or 0)
                        opportunity = {
                            **structure,
                            "score": 0.0,
                            "tier": "WATCH",
                            "risk_multiplier": 0.0,
                            "passed": False,
                            "eligible": False,
                            "expected_profit_pct": expected,
                            "cost_ratio": expected / observed_cost_pct if observed_cost_pct > 0 else 999.0,
                            "reason": "中性市场结构已生成，交由当前 V4 版本独立排序",
                        }
                        challenger = {"enabled": False, "reason": "旧 V3 实验已归档"}
                    tier = str(opportunity.get("tier") or "WATCH")
                    tier_multiplier = float(opportunity.get("risk_multiplier") or 0)
                    direction_multiplier = float(opportunity.get("direction_multiplier") or 0)
                    derivative_multiplier = float(derivatives.get("risk_multiplier", 1.0))
                    risk_pct = min(
                        float(mode["risk_pct"]),
                        float(mode["risk_pct"]) * tier_multiplier * direction_multiplier * derivative_multiplier,
                    )
                    expected_profit_pct = float(opportunity.get("expected_profit_pct") or 0)
                    cost_ratio = float(opportunity.get("cost_ratio") or 0)
                    passed = bool(opportunity.get("passed"))
                    entry_type = str(signal.get("entry_type") or "watch")
                    market_state = {
                        "enabled": True,
                        "engine": "opportunity_v3",
                        "state": opportunity.get("market_regime"),
                        "label": opportunity.get("market_regime_label"),
                        "allows_entry": bool(opportunity.get("eligible")),
                        "risk_multiplier": round(direction_multiplier, 4),
                    }
                    quality = {
                        "engine": "opportunity_v3",
                        "score": float(opportunity.get("score") or 0),
                        "allowed": tier in {"A+", "A"} and bool(opportunity.get("eligible")),
                        "pool": "trade" if tier in {"A+", "A"} else "observe",
                        "tier": tier,
                        "quality_risk_multiplier": tier_multiplier,
                        "quality_risk_reasons": [f"V3 {opportunity.get('tier_label') or tier}"],
                        "simulation": {
                            "passed": None,
                            "diagnostic": "V3 实时路径不重复运行窗口回测，统一由离线回放验证",
                        },
                    }
                    candidate = {
                        "symbol": symbol,
                        "direction": direction,
                        "mode": mode["mode"],
                        "strategy": "opportunity_v3_trend",
                        "strategy_family": V3_STRATEGY_FAMILY,
                        "strategy_version": str(config.get("opportunity_v3_strategy_version") or "v3.2"),
                        "strategy_role": "active",
                        "strategy_generation": "v3",
                        "score": round(float(opportunity.get("score") or 0), 4),
                        "passed": passed,
                        "reason": "passed" if passed else "opportunity_v3_not_ready",
                        "decision_reason": opportunity.get("reason"),
                        "entry_type": entry_type,
                        "entry_type_label": signal.get("entry_type_label", "V3 观察"),
                        "v33_challenger": challenger,
                        "v3_tier": tier,
                        "symbol_quality": quality,
                        "symbol_pool": quality["pool"],
                        "live_performance": {"enabled": False, "reason": "v3_strategy_credit_isolated"},
                        "market_state": market_state,
                        "adaptive_thresholds": {"engine": "opportunity_v3", "market": v3_market_context.get("regime")},
                        "firecracker": firecracker,
                        "derivatives": derivatives,
                        "squeeze": squeeze,
                        "depth": depth,
                        "depth_checked": depth.get("reason") != "depth_not_checked",
                        "simulation_passed": None,
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
                        "estimated_cost_pct": observed_cost_pct,
                        "observed_cost_floor_pct": observed_cost_pct,
                        "expected_profit_pct": expected_profit_pct,
                        "risk_pct": risk_pct,
                        "risk_adjustment": {
                            "type": "opportunity_v3_tier",
                            "multiplier": round(tier_multiplier * direction_multiplier * derivative_multiplier, 6),
                            "tier": tier,
                        },
                        "quality_risk_multiplier": tier_multiplier,
                        "quality_risk_reasons": quality["quality_risk_reasons"],
                        "base_risk_pct": mode["risk_pct"],
                        "leverage": mode["leverage"],
                        "margin_pct": mode["margin_pct"],
                        "thresholds": {
                            "a_plus": config.get("opportunity_v3_a_plus_score", 82.0),
                            "a": config.get("opportunity_v3_a_score", 70.0),
                            "b": config.get("opportunity_v3_b_score", 58.0),
                        },
                        "coarse": next((row for row in coarse_rows if row["symbol"] == symbol), {}),
                    }
                    candidate["market_structure"] = structure
                    if not v4_enabled:
                        candidate["opportunity_v3"] = opportunity
                        candidate = _finalize_candidate(
                            candidate,
                            config=config,
                            mode=mode,
                            exchange_filters=exchange_filters,
                            equity=equity,
                        )
                    candidates.append(candidate)
                    continue

                quality = score_symbol_quality(symbol, bars, ticker, signal, backtests, depth, config, mode)
                quality = _apply_live_performance_quality(quality, live_perf, config)
                market_state = classify_market_state(symbol, bars, signal, depth, config)
                spot_proxy = spot_proxy_confirmation(bars, direction, signal, config) if is_extreme_mode(mode["mode"]) else {"enabled": False, "score_delta": 0.0, "risk_multiplier": 1.0}
                quality_multiplier = float(quality.get("quality_risk_multiplier", 1.0))
                risk_pct *= quality_multiplier
                risk_pct *= float(market_state.get("risk_multiplier", 1.0))
                risk_pct *= float(derivatives.get("risk_multiplier", 1.0))
                risk_pct *= float(spot_proxy.get("risk_multiplier", 1.0))
                history_passed = (
                    recent["trades"] >= min_trades
                    and recent["profit_factor"] >= min_pf
                    and recent["net_pct"] > min_net_pct
                )
                if is_extreme_mode(mode["mode"]) and config.get("extreme_v2_enabled", True):
                    history_passed = history_passed or (
                        firecracker.get("is_firecracker")
                        and int(recent.get("trades", 0)) >= 1
                        and float(recent.get("net_pct", 0)) > -8.0
                        and float(recent.get("profit_factor", 0)) >= 0.45
                    )
                standard_passed = (
                    signal.get("signal") == direction
                    and history_passed
                    and quality["allowed"]
                    and market_state.get("allows_entry", True)
                    and expected_profit_pct >= float(config.get(f"{fast_prefix}_min_expected_profit_pct", 0.22) if is_sprint else config.get("min_expected_profit_pct", 0.35))
                    and cost_ratio >= float(config.get(f"{fast_prefix}_min_expected_profit_cost_ratio", 1.35) if is_sprint else config.get("min_expected_profit_cost_ratio", 3.0))
                )
                yolo_orderbook_only = (
                    mode["mode"] == "yolo_scalp"
                    and config.get("yolo_scalp_orderbook_only_enabled", True)
                )
                if yolo_orderbook_only:
                    standard_passed = False
                current_score = _current_signal_score(signal, direction, cost_ratio, recent)
                score = 0.0
                score += min(float(ticker.get("quoteVolume", 0)) / 1_000_000_000, 5) * 0.5
                score += recent["net_pct"] * 0.15
                score += min(recent["profit_factor"], 10) * 2
                score += recent["win_rate"] * 0.05
                score += current_score
                score += quality["score"] * 0.25
                score += float(firecracker.get("score", 0)) * 0.15 if firecracker.get("is_firecracker") else 0
                score += float(derivatives.get("score_delta", 0))
                score += float(spot_proxy.get("score_delta", 0))
                score += float(squeeze.get("score", 0))
                score += 3 if standard_passed else 0
                score -= 1.5 if direction == "SHORT" else 0

                entry_type = str(signal.get("entry_type") or "standard") if standard_passed else "watch"
                standard_min_score = float(config.get(f"{fast_prefix}_standard_min_score", 72.0) if is_sprint else config.get("standard_min_score", 85.0))
                passed = standard_passed and score >= standard_min_score
                risk_adjustment: dict[str, Any] | None = None
                decision_reason = "标准突破信号通过" if passed else "等待触发"
                scalp_signal: dict[str, Any] = {"enabled": False}
                if (
                    mode["mode"] == "yolo_scalp"
                    and config.get("yolo_scalp_orderbook_engine_enabled", True)
                    and not passed
                    and quality.get("pool") != "disabled"
                    and market_state.get("state") not in {"liquidity_trap", "spike_wick"}
                ):
                    scalp_signal = build_scalp_signal(
                        symbol=symbol,
                        direction=direction,
                        bars=bars,
                        ticker=ticker,
                        depth=depth,
                        event=opportunity_by_symbol.get(symbol) or trigger_by_symbol.get(symbol),
                        base_signal=signal,
                        recent=recent,
                        config=config,
                    )
                    if scalp_signal.get("passed"):
                        entry_type = str(scalp_signal.get("entry_type") or "volume_scalp")
                        signal = dict(scalp_signal["signal"])
                        expected_profit_pct = float(
                            scalp_signal.get("expected_profit_pct") or signal.get("expected_profit_pct") or 0
                        )
                        cost_ratio = float(scalp_signal.get("cost_ratio") or (expected_profit_pct / cost_pct if cost_pct else 0))
                        score = max(score, float(scalp_signal.get("score") or score) + float(quality.get("score") or 0) * 0.12)
                        if entry_type == "orderbook_impact":
                            risk_pct = float(mode["risk_pct"]) * 0.95
                        elif entry_type == "volume_scalp":
                            risk_pct = float(mode["risk_pct"]) * 0.75
                        else:
                            risk_pct = float(mode["risk_pct"]) * 0.42
                        risk_pct *= quality_multiplier
                        passed = True
                        decision_reason = (
                            f"{scalp_signal.get('label', '盘口剥头皮')}通过："
                            f"点差 {scalp_signal.get('spread_pct')}%，"
                            f"盘口失衡 {scalp_signal.get('directed_imbalance')}，"
                            f"扣费后净空间 {scalp_signal.get('net_profit_pct')}%"
                        )
                    elif scalp_signal.get("enabled") and scalp_signal.get("blockers"):
                        decision_reason = "盘口剥头皮未通过：" + "；".join(str(item) for item in scalp_signal.get("blockers", [])[:3])
                if passed and quality["pool"] == "small_trade" and not yolo_orderbook_only:
                    entry_type = "small_standard"
                    risk_pct *= float(config.get("small_trade_risk_multiplier", 0.5))
                    decision_reason = "币种质量允许小仓试探"
                if passed and quality["pool"] == "adaptive_live" and not yolo_orderbook_only:
                    entry_type = "adaptive_live_standard"
                    risk_pct *= float(config.get("live_performance_risk_multiplier", 0.6))
                    decision_reason = "recent live performance supports reduced-risk entry"
                if signal.get("signal") == direction and not quality["allowed"] and not yolo_orderbook_only:
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
                if signal.get("signal") == direction and not passed and not quality["allowed"] and not yolo_orderbook_only:
                    weak_allowed, weak_reasons = weak_quality_probe_allows_entry(
                        score,
                        quality,
                        recent,
                        signal,
                        cost_ratio,
                        depth,
                        config,
                        mode,
                    )
                    if weak_allowed:
                        entry_type = "weak_quality_probe"
                        passed = True
                        params = strategy_params_for_mode(config, mode, "weak_quality_probe")
                        weak_signal = latest_strategy_signal(
                            symbol,
                            bars,
                            mode["strategy"],
                            params=params,
                            direction=direction,
                        )
                        if weak_signal.get("signal") == direction:
                            signal = weak_signal
                            expected_profit_pct = float(signal.get("expected_profit_pct") or 0)
                            cost_ratio = expected_profit_pct / cost_pct if cost_pct else 0
                        signal["entry_type"] = "weak_quality_probe"
                        signal["entry_type_label"] = "弱质量试探"
                        if params:
                            signal["protection_profile"] = {
                                "stop_atr": params.stop_atr,
                                "take_profit_atr": params.take_profit_atr,
                                "max_hold_bars": params.max_hold_bars,
                            }
                        risk_adjustment = weak_quality_probe_risk_adjustment(quality, recent, depth, config)
                        risk_pct = float(mode["risk_pct"]) * float(risk_adjustment["multiplier"])
                        decision_reason = "弱质量试探：候选信号强，但币种质量仍在观察池；使用小仓位获取实盘样本；" + "；".join(risk_adjustment["reasons"])
                    elif weak_reasons:
                        decision_reason += "；弱质量试探未通过：" + "；".join(weak_reasons[:3])
                preemptive_enabled = (
                    mode["mode"] in {"tournament", "tournament_sprint", "extreme_sprint", "yolo_scalp"}
                    and config.get("preemptive_entries_enabled", True)
                    and not yolo_orderbook_only
                )
                if (
                    not passed
                    and is_extreme_mode(mode["mode"])
                    and not yolo_orderbook_only
                    and config.get("extreme_v2_enabled", True)
                    and config.get("extreme_probe_enabled", True)
                    and firecracker.get("is_firecracker")
                    and score >= float(config.get("extreme_probe_min_score", 78.0))
                    and float(firecracker.get("score", 0)) >= float(config.get("extreme_probe_min_firecracker_score", 55.0))
                    and market_state.get("state") not in {"liquidity_trap", "spike_wick"}
                    and float(derivatives.get("risk_multiplier", 1.0)) > 0
                ):
                    probe_signal_ok = signal.get("signal") == direction or (
                        signal.get("trend") is True
                        and signal.get("volatility_ok") is True
                        and (
                            float(signal.get("distance_to_trigger_pct") or 999) <= float(config.get(f"{fast_prefix}_preemptive_max_distance_pct", config.get("extreme_sprint_preemptive_max_distance_pct", 0.55))) * 2
                            or bool(squeeze.get("active"))
                            or bool(squeeze.get("released"))
                        )
                    )
                    if probe_signal_ok:
                        if signal.get("signal") != direction:
                            signal = _promote_wait_signal(
                                signal,
                                direction,
                                "extreme_probe",
                                float(config.get("extreme_probe_risk_multiplier", 0.22)),
                                params=strategy_params_for_mode(config, mode, "extreme_probe"),
                            )
                            expected_profit_pct = float(signal.get("expected_profit_pct") or 0)
                            cost_ratio = expected_profit_pct / cost_pct if cost_pct else 0
                        probe_cost_ok = cost_ratio >= float(config.get("extreme_probe_min_expected_profit_cost_ratio", 1.05))
                        recent_ok = float(recent.get("net_pct", 0)) > -12.0 and float(recent.get("profit_factor", 0)) >= 0.2
                        if probe_cost_ok and recent_ok:
                            entry_type = "extreme_probe"
                            passed = True
                            risk_pct = min(
                                float(mode["risk_pct"]) * float(config.get("extreme_probe_risk_multiplier", 0.22)) * quality_multiplier * float(derivatives.get("risk_multiplier", 1.0)) * float(spot_proxy.get("risk_multiplier", 1.0)),
                                float(config.get("extreme_probe_max_risk_pct", 6.0)),
                            )
                            if derivatives.get("enabled") and not derivatives.get("confirmed"):
                                unconfirmed_mult = float(config.get("extreme_probe_unconfirmed_derivative_risk_multiplier", 0.55))
                                risk_pct *= unconfirmed_mult
                                risk_pct = min(
                                    risk_pct,
                                    float(config.get("extreme_probe_unconfirmed_derivative_max_risk_pct", 1.5)),
                                )
                                risk_adjustment = {
                                    "type": "extreme_probe_unconfirmed_derivatives",
                                    "multiplier": unconfirmed_mult,
                                    "max_risk_pct": float(config.get("extreme_probe_unconfirmed_derivative_max_risk_pct", 1.5)),
                                    "derivatives": derivatives,
                                }
                            if market_state.get("state") == "chop":
                                risk_pct *= 0.8
                            decision_reason = "极限V2火药桶小仓试探：放量/异动满足，先用小仓积累机会"
                        else:
                            decision_reason = "极限V2试探未通过：成本比或近期表现不足"
                if not passed and preemptive_enabled and history_passed and quality["allowed"] and market_state.get("allows_entry", True) and signal.get("signal") == "WAIT":
                    distance_pct = float(signal.get("distance_to_trigger_pct") or 999)
                    max_distance = float(config.get(f"{fast_prefix}_preemptive_max_distance_pct", 0.55) if is_sprint else config.get("preemptive_max_distance_pct", 0.35))
                    min_candle_pct = float(config.get(f"{fast_prefix}_momentum_min_candle_pct", 0.10)) if is_sprint else 0.12
                    near_trigger = (
                        signal.get("trend") is True
                        and signal.get("volatility_ok") is True
                        and distance_pct <= max_distance
                    )
                    strong_momentum = (
                        (config.get(f"{fast_prefix}_momentum_enabled", True) if is_sprint else True)
                        and signal.get("trend") is True
                        and signal.get("volatility_ok") is True
                        and float(signal.get("candle_move_pct") or 0) >= max(min_candle_pct, distance_pct)
                    )
                    min_preempt_score = float(config.get(f"{fast_prefix}_preemptive_min_score", 58.0) if is_sprint else config.get("preemptive_min_score", 72.0))
                    min_momentum_score = float(config.get(f"{fast_prefix}_momentum_min_score", min_preempt_score) if is_sprint else min_preempt_score)
                    if (near_trigger and score >= min_preempt_score) or (strong_momentum and score >= min_momentum_score):
                        entry_type = "momentum" if strong_momentum and not near_trigger else "preemptive"
                        risk_multiplier = (
                            float(config.get(f"{fast_prefix}_short_preemptive_risk_multiplier", 0.25) if is_sprint else config.get("short_preemptive_risk_multiplier", 0.18))
                            if direction == "SHORT"
                            else float(config.get(f"{fast_prefix}_preemptive_risk_multiplier", 0.35) if is_sprint else config.get("preemptive_risk_multiplier", 0.24))
                        )
                        if quality["pool"] == "small_trade":
                            risk_multiplier *= float(config.get("small_trade_risk_multiplier", 0.5))
                        entry_params = strategy_params_for_mode(config, mode, entry_type)
                        signal = _promote_wait_signal(signal, direction, entry_type, risk_multiplier, params=entry_params)
                        expected_profit_pct = float(signal.get("expected_profit_pct") or 0)
                        cost_ratio = expected_profit_pct / cost_pct if cost_pct else 0
                        risk_pct *= risk_multiplier
                        passed = (
                            expected_profit_pct >= float(config.get(f"{fast_prefix}_min_expected_profit_pct", 0.22) if is_sprint else config.get("min_expected_profit_pct", 0.35))
                            and cost_ratio >= (
                                float(config.get(f"{fast_prefix}_min_expected_profit_cost_ratio", 1.35))
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

                extreme_risk_multiplier = 1.0
                if is_extreme_mode(mode["mode"]) and passed and entry_type not in {"extreme_probe", "weak_quality_probe"}:
                    if score >= float(config.get(f"{fast_prefix}_super_score", config.get("extreme_sprint_super_score", 135.0))):
                        extreme_risk_multiplier = float(config.get(f"{fast_prefix}_super_risk_multiplier", config.get("extreme_sprint_super_risk_multiplier", 1.75)))
                    elif score >= float(config.get(f"{fast_prefix}_high_score", config.get("extreme_sprint_high_score", 110.0))):
                        extreme_risk_multiplier = float(config.get(f"{fast_prefix}_high_risk_multiplier", config.get("extreme_sprint_high_risk_multiplier", 1.35)))
                    risk_pct *= extreme_risk_multiplier
                v2_tier = (
                    "冲刺"
                    if passed and entry_type in {"standard", "small_standard", "adaptive_live_standard"}
                    else "试探"
                    if entry_type in {"extreme_probe", "weak_quality_probe"}
                    else "观察"
                )

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
                        "market_state": market_state,
                        "adaptive_thresholds": market_state.get("adaptive_thresholds") or adaptive_thresholds(bars, config),
                        "scalp_signal": scalp_signal,
                        "firecracker": firecracker,
                        "derivatives": derivatives,
                        "spot_proxy": spot_proxy,
                        "squeeze": squeeze,
                        "v2_tier": v2_tier,
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
                        "extreme_risk_multiplier": extreme_risk_multiplier,
                        "risk_adjustment": risk_adjustment,
                        "quality_risk_multiplier": quality.get("quality_risk_multiplier", 1.0),
                        "quality_risk_reasons": quality.get("quality_risk_reasons", []),
                        "base_risk_pct": mode["risk_pct"],
                        "leverage": mode["leverage"],
                        "margin_pct": mode["margin_pct"],
                        "thresholds": {"min_trades": min_trades, "min_pf": min_pf, "min_net_pct": min_net_pct},
                        "coarse": next((row for row in coarse_rows if row["symbol"] == symbol), {}),
                    }
                candidate = _finalize_candidate(
                    candidate,
                    config=config,
                    mode=mode,
                    exchange_filters=exchange_filters,
                    equity=equity,
                )
                candidates.append(candidate)
        except Exception as exc:
            candidates.append({"symbol": symbol, "passed": False, "reason": str(exc), "score": -999})

    candidates = attach_v4_rankings(candidates, config)
    if v3_enabled and config.get("opportunity_v4_enabled", True):
        candidates = [
            _finalize_candidate(
                _apply_v4_live_selection(candidate, config, mode),
                config=config,
                mode=mode,
                exchange_filters=exchange_filters,
                equity=equity,
            )
            for candidate in candidates
        ]
    candidates.sort(
        key=(
            lambda item: (
                item.get("passed", False),
                bool((item.get("opportunity_v4") or {}).get("decision_candidate")),
                float((item.get("opportunity_v4") or {}).get("lower_expected_net_pct") or -999),
                float((item.get("opportunity_v4") or {}).get("rank_percentile") or -1),
                item.get("score", -999),
            )
            if config.get("opportunity_v4_enabled", True)
            else (
                item.get("passed", False),
                item.get("symbol_quality", {}).get("allowed", False),
                item.get("symbol_quality", {}).get("score", -999),
                item.get("score", -999),
            )
        ),
        reverse=True,
    )
    candidates = [_json_safe(candidate) for candidate in candidates]
    max_candidates = int(config.get("max_scan_symbols", 30))
    v4_candidates = sorted(
        [candidate for candidate in candidates if (candidate.get("opportunity_v4") or {}).get("shadow_eligible")],
        key=lambda item: (
            bool((item.get("opportunity_v4") or {}).get("decision_candidate")),
            float((item.get("opportunity_v4") or {}).get("lower_expected_net_pct") or -999),
        ),
        reverse=True,
    )[: max(max_candidates, int(config.get("opportunity_v4_decision_shadow_limit", 3)) + int(config.get("opportunity_v4_exploration_shadow_limit", 6)) + 6)]
    trade_pool = [
        candidate
        for candidate in candidates
        if candidate.get("symbol_quality", {}).get("pool") in {"trade", "small_trade", "adaptive_live", "observe_hot"}
        or candidate.get("entry_type") in {"extreme_probe", "weak_quality_probe", "orderbook_impact", "volume_scalp", "imbalance_probe"}
    ][: int(config.get("max_trade_pool_symbols", 15))]
    observe_pool = [
        candidate
        for candidate in candidates
        if candidate.get("symbol_quality", {}).get("pool") == "observe"
    ][:max_candidates]
    firecracker_count = sum(1 for candidate in candidates if candidate.get("firecracker", {}).get("is_firecracker"))
    probe_count = sum(1 for candidate in candidates if candidate.get("entry_type") in {"extreme_probe", "weak_quality_probe"})
    scalp_count = sum(1 for candidate in candidates if candidate.get("entry_type") in {"orderbook_impact", "volume_scalp", "imbalance_probe"})
    sprint_count = sum(1 for candidate in candidates if candidate.get("passed") and candidate.get("entry_type") not in {"extreme_probe", "weak_quality_probe"})
    v4_shadow_ready = sum(1 for candidate in candidates if (candidate.get("opportunity_v4") or {}).get("shadow_eligible"))
    v4_admitted = sum(
        1
        for candidate in candidates
        if candidate.get("passed") and (candidate.get("opportunity_v4") or {}).get("admitted")
    )
    v44_active = s0_full_bet_profile_active(config)
    v43_canary_ready = sum(
        1
        for candidate in candidates
        if (
            (candidate.get("opportunity_v4") or {}).get("full_bet_admitted")
            if v44_active
            else (candidate.get("opportunity_v4") or {}).get("canary_eligible")
        )
    )
    v43_validated = sum(1 for candidate in candidates if (candidate.get("opportunity_v4") or {}).get("validated"))
    v43_provisional = sum(1 for candidate in candidates if (candidate.get("opportunity_v4") or {}).get("provisional"))
    v43_exploration = sum(1 for candidate in candidates if (candidate.get("opportunity_v4") or {}).get("exploration_admitted"))
    v431_local_blocked = sum(
        1
        for candidate in candidates
        if ((candidate.get("opportunity_v4") or {}).get("local_circuit") or {}).get("blocked")
    )
    blocked_reasons: dict[str, int] = {}
    blocked_categories: dict[str, int] = {}
    for candidate in candidates:
        if candidate.get("passed"):
            continue
        reason = str(candidate.get("reason") or "unknown")
        v4 = candidate.get("opportunity_v4") or {}
        v4_blockers = list(v4.get("blockers") or [])
        market_reason = str(candidate.get("market_state", {}).get("state") or "")
        pool_reason = str(candidate.get("symbol_quality", {}).get("pool") or "")
        key = (
            str(v4_blockers[0])
            if v4_blockers
            else market_reason
            if market_reason in {"chop", "liquidity_trap", "spike_wick"}
            else pool_reason
            if pool_reason == "disabled"
            else reason
        )
        blocked_reasons[key] = blocked_reasons.get(key, 0) + 1
        blocker_text = " ".join(str(item) for item in v4_blockers)
        policy = v4.get("regime_policy") or {}
        if not v44_active and v4.get("evidence_status") == "blocked_negative":
            category = "同形态局部负证据"
        elif not any(policy.get(name) for name in ("live_scope", "canary_scope", "exploration_scope")):
            category = "市场方向或入场结构"
        elif "排名" in blocker_text:
            category = "本轮排名未达标"
        elif any(token in blocker_text for token in ("期望", "成本比")):
            category = "扣费后期望或成本"
        elif "确认不足" in blocker_text:
            category = "量价确认不足"
        elif v4.get("liquidity_gate") and not (v4.get("liquidity_gate") or {}).get("passed"):
            category = "盘口流动性或点差"
        elif (candidate.get("execution_filter") or {}).get("enabled") and not (
            candidate.get("execution_filter") or {}
        ).get("executable"):
            category = "交易所最小下单量"
        else:
            category = "其他候选条件"
        blocked_categories[category] = blocked_categories.get(category, 0) + 1
    if not fast_lane:
        _publish_stream_intent(config, account_summary, coarse_rows, candidates, trade_pool)
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
        "extreme_v2": {
            "enabled": bool(is_extreme_mode(mode.get("mode")) and config.get("extreme_v2_enabled", True) and not v3_enabled),
            "firecracker": firecracker_count,
            "probe": probe_count,
            "scalp": scalp_count,
            "sprint": sprint_count,
            "blocked_reasons": blocked_reasons,
            "label": "极限V2",
        },
        "market_structure": {
            "enabled": bool(v3_enabled),
            "schema_version": "market-structure-v1",
            "market_regime": v3_market_context.get("regime"),
            "market_label": v3_market_context.get("label"),
            "breadth_positive": v3_market_context.get("breadth_positive"),
            "dispersion_pct": v3_market_context.get("dispersion_pct"),
            "observed_cost_floor_pct": round(observed_cost_pct, 6),
            "label": "市场结构特征",
        },
        "opportunity_v4": {
            "enabled": bool(config.get("opportunity_v4_enabled", True)),
            "strategy_family": V4_STRATEGY_FAMILY,
            "strategy_version": str(config.get("opportunity_v4_strategy_version") or "v4.3.2"),
            "shadow_ready": v4_shadow_ready,
            "admitted": v4_admitted,
            "canary_ready": v43_canary_ready,
            "provisional": v43_provisional,
            "validated": v43_validated,
            "exploration_admitted": v43_exploration,
            "local_circuit_blocked": v431_local_blocked,
            "blocked_reasons": blocked_reasons,
            "blocked_categories": blocked_categories,
            "structure_ready": sum(
                1
                for candidate in candidates
                if any(
                    ((candidate.get("opportunity_v4") or {}).get("regime_policy") or {}).get(name)
                    for name in ("live_scope", "canary_scope", "exploration_scope")
                )
            ),
            "liquidity_ready": sum(
                1
                for candidate in candidates
                if ((candidate.get("opportunity_v4") or {}).get("liquidity_gate") or {}).get("passed")
            ),
            "live_enabled": bool(config.get("opportunity_v4_live_enabled", False)),
            "label": (
                "V4.5 单仓全进全出、相对排名与五项确认"
                if v44_active
                else "V4.3.2 连续质量仓位、局部熔断与顺势双通道排序"
            ),
        },
        "opportunity_queue": {
            "enabled": bool(config.get("opportunity_queue_enabled", True)),
            "count": len(opportunity_events),
            "symbols": opportunity_symbols[:20],
            "label": "事件队列",
        },
        "adaptive_market": market_profile,
        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
        "channel": "fast_lane" if fast_lane else "background_scan",
        "degrade_seconds": degrade_seconds,
        "coarse_top": _json_safe(coarse_rows[:20]),
    }
    return {
        "mode": _json_safe(mode),
        "symbols": processed_symbols,
        "ranked_symbols": ranked_symbols,
        "recalled_symbols": recalled_symbols,
        "opportunity_events": _json_safe(opportunity_events[:20]),
        "funnel": _json_safe(funnel),
        "trade_pool": trade_pool,
        "observe_pool": observe_pool,
        "candidates": candidates[:max_candidates],
        "v4_candidates": v4_candidates,
        "best": candidates[0] if candidates else None,
    }


def _publish_stream_intent(
    config: dict[str, Any],
    account_summary: dict[str, Any],
    coarse_rows: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    trade_pool: list[dict[str, Any]],
) -> None:
    if not config.get("market_stream_dynamic_enabled", True):
        return
    try:
        hot_limit = int(config.get("stream_hot_symbols_limit", 25))
        hot_symbols = [str(row.get("symbol", "")) for row in coarse_rows[:hot_limit]]
        candidate_symbols = [
            str(item.get("symbol", ""))
            for item in sorted(
                candidates,
                key=lambda row: (
                    row.get("passed", False),
                    row.get("symbol_quality", {}).get("allowed", False),
                    row.get("score", -999),
                ),
                reverse=True,
            )[:hot_limit]
        ]
        candidate_symbols = [str(item.get("symbol", "")) for item in trade_pool] + candidate_symbols
        position_symbols = [
            str(position.get("symbol", ""))
            for position in account_summary.get("positions", []) or []
            if abs(float(position.get("positionAmt") or position.get("position_amount") or 0)) > 0
        ]
        live_credit_symbols = [
            str(item.get("symbol", ""))
            for item in list_live_scores(int(config.get("live_credit_sync_max_symbols", 20)), config)
            if float(item.get("risk_multiplier") or 0) > 0
        ]
        write_stream_intent(
            hot_symbols=hot_symbols,
            candidate_symbols=candidate_symbols,
            position_symbols=position_symbols,
            live_credit_symbols=live_credit_symbols,
            shadow_symbols=active_shadow_symbols(int(config.get("shadow_stream_symbols_limit", 100))),
            active_mode=active_growth_mode(config),
        )
    except Exception:
        return


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return 999.0 if value > 0 else 0.0
    return value
