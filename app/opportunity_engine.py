from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from statistics import median
from typing import Any

from app.strategy import atr, ema


V3_STRATEGY_FAMILY = "extreme_v3_roll"
V31_CHALLENGER_FAMILY = "extreme_v31_challenger"
V33_CHALLENGER_VERSION = "v3.3-candidate"
V3_ENTRY_TYPES = {"v3_breakout", "v3_pullback", "v3_momentum", "v3_prebreakout"}


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


def _percentile(values: list[float], fraction: float) -> float:
    clean = sorted(value for value in values if math.isfinite(value))
    if not clean:
        return 0.0
    position = (len(clean) - 1) * _clamp(fraction, 0.0, 1.0)
    lower = int(position)
    upper = min(lower + 1, len(clean) - 1)
    weight = position - lower
    return clean[lower] * (1 - weight) + clean[upper] * weight


def _percentile_rank(values: list[float], value: float) -> float:
    clean = sorted(item for item in values if math.isfinite(item))
    if not clean:
        return 0.5
    below = sum(1 for item in clean if item < value)
    equal = sum(1 for item in clean if item == value)
    return _clamp((below + equal * 0.5) / len(clean), 0.0, 1.0)


def _ticker_rows(tickers: list[dict[str, Any]] | dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = list(tickers.values()) if isinstance(tickers, dict) else list(tickers)
    result = []
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol.endswith("USDT"):
            continue
        try:
            change = float(row.get("priceChangePercent") or 0)
            volume = float(row.get("quoteVolume") or 0)
            price = float(row.get("lastPrice") or row.get("last") or 0)
        except (TypeError, ValueError):
            continue
        if volume <= 0 or price <= 0:
            continue
        result.append({"symbol": symbol, "change_pct": change, "quote_volume": volume, "last_price": price})
    return result


def build_market_context(
    tickers: list[dict[str, Any]] | dict[str, dict[str, Any]],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a cross-sectional market state without making exchange requests."""
    config = config or {}
    rows = _ticker_rows(tickers)
    changes = [row["change_pct"] for row in rows]
    abs_changes = [abs(value) for value in changes]
    log_volumes = [math.log10(max(row["quote_volume"], 1.0)) for row in rows]
    breadth = sum(1 for value in changes if value > 0) / len(changes) if changes else 0.5
    median_change = median(changes) if changes else 0.0
    q25 = _percentile(changes, 0.25)
    q75 = _percentile(changes, 0.75)
    dispersion = q75 - q25
    median_abs = median(abs_changes) if abs_changes else 0.0
    p90_abs = _percentile(abs_changes, 0.90)
    references = {row["symbol"]: row["change_pct"] for row in rows if row["symbol"] in {"BTCUSDT", "ETHUSDT"}}
    reference_change = sum(references.values()) / len(references) if references else median_change

    panic_breadth = float(config.get("opportunity_v3_panic_breadth", 0.20))
    trend_breadth = float(config.get("opportunity_v3_trend_breadth", 0.62))
    trend_move = max(0.6, float(config.get("opportunity_v3_market_trend_move_pct", 0.8)))
    rotation_dispersion = max(
        float(config.get("opportunity_v3_rotation_dispersion_pct", 7.0)),
        median_abs * 2.2,
    )
    if breadth <= panic_breadth and median_change <= -3.0:
        regime, label = "panic", "恐慌下跌"
    elif breadth <= 1.0 - trend_breadth and median_change <= -trend_move and reference_change < 0:
        regime, label = "broad_down", "广泛下跌趋势"
    elif breadth >= trend_breadth and median_change >= trend_move and reference_change > 0:
        regime, label = "broad_up", "广泛上涨趋势"
    elif dispersion >= rotation_dispersion:
        regime, label = "rotation", "高离散轮动"
    elif median_abs <= float(config.get("opportunity_v3_quiet_median_abs_pct", 1.8)):
        regime, label = "quiet", "低波动震荡"
    else:
        regime, label = "mixed", "混合震荡"

    symbol_context: dict[str, dict[str, Any]] = {}
    for row in rows:
        change = row["change_pct"]
        log_volume = math.log10(max(row["quote_volume"], 1.0))
        long_rank = _percentile_rank(changes, change)
        symbol_context[row["symbol"]] = {
            **row,
            "long_strength_percentile": round(long_rank, 6),
            "short_strength_percentile": round(1.0 - long_rank, 6),
            "volume_percentile": round(_percentile_rank(log_volumes, log_volume), 6),
            "absolute_move_percentile": round(_percentile_rank(abs_changes, abs(change)), 6),
            "residual_change_pct": round(change - median_change, 6),
        }

    direction_multipliers = {
        "broad_up": {"LONG": 1.10, "SHORT": 0.65},
        "broad_down": {"LONG": 0.65, "SHORT": 1.08},
        "panic": {"LONG": 0.30, "SHORT": 0.78},
        "rotation": {"LONG": 0.92, "SHORT": 0.92},
        "quiet": {"LONG": 0.72, "SHORT": 0.72},
        "mixed": {"LONG": 0.85, "SHORT": 0.85},
    }[regime]
    return {
        "engine": "opportunity_v3",
        "regime": regime,
        "label": label,
        "sample_size": len(rows),
        "breadth_positive": round(breadth, 6),
        "median_change_pct": round(median_change, 6),
        "median_abs_change_pct": round(median_abs, 6),
        "p25_change_pct": round(q25, 6),
        "p75_change_pct": round(q75, 6),
        "p90_abs_change_pct": round(p90_abs, 6),
        "dispersion_pct": round(dispersion, 6),
        "reference_change_pct": round(reference_change, 6),
        "direction_multipliers": direction_multipliers,
        "symbols": symbol_context,
    }


def _quote_volume(row: list[Any]) -> float:
    if len(row) > 7:
        try:
            value = float(row[7] or 0)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return float(row[5] or 0) * float(row[4] or 0)


def _volume_acceleration(bars: list[list[Any]], lookback: int = 20) -> float:
    if len(bars) < lookback + 1:
        return 1.0
    history = [_quote_volume(row) for row in bars[-lookback - 1 : -1]]
    baseline = median([value for value in history if value > 0]) if any(value > 0 for value in history) else 0.0
    current = _quote_volume(bars[-1])
    try:
        open_time = int(bars[-1][0])
        close_time = int(bars[-1][6])
        if open_time <= int(time.time() * 1000) <= close_time and close_time > open_time:
            elapsed = (int(time.time() * 1000) - open_time) / (close_time - open_time)
            current /= max(0.15, min(elapsed, 1.0))
    except (TypeError, ValueError, IndexError):
        pass
    return current / baseline if baseline > 0 else 1.0


def _taker_buy_ratio(row: list[Any]) -> float:
    try:
        total = float(row[7] or 0)
        buy = float(row[10] or 0)
    except (TypeError, ValueError, IndexError):
        return 0.5
    return _clamp(buy / total, 0.0, 1.0) if total > 0 else 0.5


def build_v3_signal(
    symbol: str,
    bars: list[list[Any]],
    direction: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    direction = direction.upper()
    if len(bars) < 80 or direction not in {"LONG", "SHORT"}:
        return {"enabled": True, "symbol": symbol, "signal": "WAIT", "reason": "v3_not_enough_data"}
    closes = [float(row[4]) for row in bars]
    highs = [float(row[2]) for row in bars]
    lows = [float(row[3]) for row in bars]
    e10 = ema(closes, 10)
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    atr_value = float(atr(bars, 20)[-1] or 0)
    close = closes[-1]
    if close <= 0 or atr_value <= 0:
        return {"enabled": True, "symbol": symbol, "signal": "WAIT", "reason": "v3_bad_price_or_atr"}
    is_short = direction == "SHORT"
    aligned = close < e10[-1] < e20[-1] < e50[-1] if is_short else close > e10[-1] > e20[-1] > e50[-1]
    slope = (e20[-1] - e20[-4]) / close
    slope_ok = slope < 0 if is_short else slope > 0
    lookbacks = [int(value) for value in config.get("opportunity_v3_donchian_lookbacks", [12, 24, 48]) if int(value) >= 3]
    votes = 0
    distances: list[float] = []
    extensions: list[float] = []
    for lookback in lookbacks:
        if len(bars) <= lookback:
            continue
        upper = max(highs[-lookback - 1 : -1])
        lower = min(lows[-lookback - 1 : -1])
        if is_short:
            votes += int(close <= lower)
            distances.append(max(0.0, close - lower) / atr_value)
            extensions.append(max(0.0, lower - close) / atr_value)
        else:
            votes += int(close >= upper)
            distances.append(max(0.0, upper - close) / atr_value)
            extensions.append(max(0.0, close - upper) / atr_value)
    distance_atr = min(distances) if distances else 999.0
    breakout_extension_atr = max(extensions) if extensions else 0.0
    volume_acceleration = _volume_acceleration(bars)
    buy_ratio = _taker_buy_ratio(bars[-1])
    directed_flow = 1.0 - buy_ratio if is_short else buy_ratio
    signed_returns = []
    for horizon in (1, 3, 12, 48):
        if len(closes) <= horizon:
            signed_returns.append(0.0)
            continue
        move = (close - closes[-horizon - 1]) / close * 100
        signed_returns.append(-move if is_short else move)
    previous_three_high = max(highs[-4:-1])
    previous_three_low = min(lows[-4:-1])
    momentum_break = close < previous_three_low if is_short else close > previous_three_high
    touched = highs[-1] >= e10[-1] * 0.998 if is_short else lows[-1] <= e10[-1] * 1.002
    recovered = close < e10[-1] and close < closes[-2] if is_short else close > e10[-1] and close > closes[-2]
    pullback = aligned and slope_ok and touched and recovered
    dynamic_volume_min = float(config.get("opportunity_v3_volume_acceleration_min", 1.05))
    setup_type = "watch"
    if aligned and slope_ok and votes >= 1 and volume_acceleration >= dynamic_volume_min:
        setup_type = "v3_breakout"
    elif pullback and directed_flow >= 0.48:
        setup_type = "v3_pullback"
    elif aligned and slope_ok and momentum_break and volume_acceleration >= max(1.15, dynamic_volume_min):
        setup_type = "v3_momentum"
    elif aligned and slope_ok and distance_atr <= float(config.get("opportunity_v3_prebreakout_distance_atr", 0.35)) and volume_acceleration >= 0.85:
        setup_type = "v3_prebreakout"

    profiles = {
        "v3_breakout": (0.90, 2.20, 18, "新趋势突破"),
        "v3_pullback": (0.78, 1.80, 12, "趋势回踩续跑"),
        "v3_momentum": (0.82, 1.65, 10, "量价加速"),
        "v3_prebreakout": (0.68, 1.30, 6, "突破前抢跑"),
    }
    stop_mult, take_mult, max_hold, label = profiles.get(setup_type, (0.9, 1.8, 10, "观察"))
    swing_distance = (
        max(highs[-7:]) - close if is_short else close - min(lows[-7:])
    )
    stop_distance = max(atr_value * stop_mult, min(max(swing_distance, 0.0), atr_value * 1.40))
    stop = close + stop_distance if is_short else close - stop_distance
    take_profit = close - atr_value * take_mult if is_short else close + atr_value * take_mult
    body = abs(close - float(bars[-1][1]))
    adverse_wick = (
        max(0.0, min(float(bars[-1][1]), close) - lows[-1])
        if is_short
        else max(0.0, highs[-1] - max(float(bars[-1][1]), close))
    )
    wick_ratio = adverse_wick / max(body, close * 0.0001)
    signal_ready = setup_type in V3_ENTRY_TYPES
    entry_phase = {
        "v3_prebreakout": "ARMED",
        "v3_pullback": "RETEST",
        "v3_breakout": "TRIGGERED",
        "v3_momentum": "TRIGGERED",
    }.get(setup_type, "WATCH")
    return {
        "enabled": True,
        "engine": "opportunity_v3",
        "strategy_family": V3_STRATEGY_FAMILY,
        "strategy_version": str(config.get("opportunity_v3_strategy_version") or "v3.2"),
        "strategy_role": "active",
        "symbol": symbol,
        "signal": direction if signal_ready else "WAIT",
        "direction": direction,
        "reason": setup_type if signal_ready else "v3_waiting_for_structure",
        "strategy": "opportunity_v3_trend",
        "entry_type": setup_type,
        "entry_type_label": label,
        "entry_phase": entry_phase,
        "last_price": close,
        "atr": atr_value,
        "atr_pct": atr_value / close * 100,
        "stop": stop,
        "take_profit": take_profit,
        "risk_pct": abs(close - stop) / close,
        "expected_profit_pct": abs(take_profit - close) / close * 100,
        "trend": aligned and slope_ok,
        "trigger": signal_ready,
        "volatility_ok": True,
        "ema_fast": e20[-1],
        "ema_slow": e50[-1],
        "donchian_votes": votes,
        "donchian_models": len(lookbacks),
        "distance_to_trigger_atr": round(distance_atr, 6),
        "breakout_extension_atr": round(breakout_extension_atr, 6),
        "distance_to_trigger_pct": round(distance_atr * atr_value / close * 100, 6) if distance_atr < 999 else 999.0,
        "volume_acceleration": round(volume_acceleration, 6),
        "directed_trade_flow": round(directed_flow, 6),
        "multi_horizon_returns": [round(value, 6) for value in signed_returns],
        "candle_move_pct": round(abs(close - closes[-2]) / close * 100, 6),
        "impulse_atr": round(((-1 if is_short else 1) * (close - closes[-2])) / atr_value, 6),
        "adverse_wick_ratio": round(wick_ratio, 6),
        "protection_profile": {
            "stop_atr": stop_distance / atr_value,
            "take_profit_atr": take_mult,
            "max_hold_bars": max_hold,
            "break_even_atr": 0.85,
            "trailing_trigger_atr": 1.10,
            "trailing_distance_atr": 0.70,
        },
    }


def _event_age_seconds(event: dict[str, Any] | None) -> float | None:
    if not event:
        return None
    value = event.get("updated_at") or event.get("created_at") or event.get("ts")
    if not value:
        return None
    try:
        if isinstance(value, (int, float)) or str(value).replace(".", "", 1).isdigit():
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            return max(0.0, time.time() - timestamp)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())
    except ValueError:
        return None


def score_v3_opportunity(
    *,
    symbol: str,
    direction: str,
    signal: dict[str, Any],
    ticker: dict[str, Any],
    market_context: dict[str, Any],
    medium_context: dict[str, dict[str, float | bool]] | None = None,
    depth: dict[str, Any],
    derivatives: dict[str, Any],
    event: dict[str, Any] | None,
    cost_pct: float,
    config: dict[str, Any],
) -> dict[str, Any]:
    direction = direction.upper()
    medium_context = medium_context or {}
    symbol_context = (market_context.get("symbols") or {}).get(symbol.upper(), {})
    medium = medium_context.get(symbol.upper()) or {}
    medium_trend_aligned = bool(medium.get("trend_short" if direction == "SHORT" else "trend_long"))
    medium_path_efficiency = float(medium.get("path_efficiency") or 0.0)
    medium_ready = bool(medium)
    strength_key = "short_strength_percentile" if direction == "SHORT" else "long_strength_percentile"
    strength = float(symbol_context.get(strength_key) or 0.5)
    relative = strength * 25.0
    trend = 0.0
    if signal.get("trend"):
        trend += 10.0
    trend += min(float(signal.get("donchian_votes") or 0) / max(float(signal.get("donchian_models") or 1), 1.0), 1.0) * 7.0
    positive_horizons = sum(1 for value in signal.get("multi_horizon_returns", []) if float(value) > 0)
    trend += min(positive_horizons / 4.0, 1.0) * 3.0
    volume_acceleration = float(signal.get("volume_acceleration") or 1.0)
    directed_flow = float(signal.get("directed_trade_flow") or 0.5)
    activity = _clamp((volume_acceleration - 0.75) / 1.75, 0.0, 1.0) * 10.0
    activity += _clamp((directed_flow - 0.45) / 0.20, 0.0, 1.0) * 5.0
    entry_type = str(signal.get("entry_type") or "watch")
    breakout = {
        "v3_breakout": 15.0,
        "v3_pullback": 13.0,
        "v3_momentum": 12.0,
        "v3_prebreakout": 8.0,
    }.get(entry_type, 0.0)
    regime = str(market_context.get("regime") or "mixed")
    direction_multiplier = float((market_context.get("direction_multipliers") or {}).get(direction, 0.85))
    regime_component = _clamp(direction_multiplier / 1.10, 0.0, 1.0) * 10.0
    derivative_component = 4.0
    if derivatives.get("enabled"):
        derivative_component = 8.0 if derivatives.get("confirmed") else _clamp(4.0 + float(derivatives.get("score_delta") or 0) * 0.25, 0.0, 8.0)
    spread = float(depth.get("spread_pct") or 999)
    depth_notional = float(depth.get("depth_notional") or 0)
    depth_known = depth.get("reason") != "depth_not_checked" and depth.get("available", True) is not False
    if depth_known:
        max_spread = max(float(config.get("opportunity_v3_max_spread_pct", 0.10)), 0.0001)
        min_depth = max(float(config.get("opportunity_v3_min_depth_notional_usdt", 5_000.0)), 1.0)
        liquidity = _clamp(1.0 - spread / (max_spread * 1.5), 0.0, 1.0) * 4.0
        liquidity += _clamp(depth_notional / min_depth, 0.0, 1.0) * 3.0
    else:
        liquidity = 0.0

    components = {
        "relative_strength": relative,
        "trend_structure": trend,
        "volume_flow": activity,
        "breakout_quality": breakout,
        "regime_fit": regime_component,
        "derivatives": derivative_component,
        "liquidity": liquidity,
    }
    if medium_ready:
        components["medium_horizon"] = (
            8.0 + _clamp(medium_path_efficiency / 0.35, 0.0, 1.0) * 4.0
            if medium_trend_aligned
            else -10.0
        )
    penalties: dict[str, float] = {}
    if float(signal.get("adverse_wick_ratio") or 0) > 2.0:
        penalties["adverse_wick"] = 10.0
    if float(symbol_context.get("absolute_move_percentile") or 0) >= 0.985 and float(signal.get("impulse_atr") or 0) >= 2.0:
        penalties["exhaustion"] = 12.0
    event_age = _event_age_seconds(event)
    if event_age is not None and event_age > float(config.get("opportunity_v3_event_fresh_seconds", 90)):
        penalties["stale_event"] = min(8.0, event_age / 60.0)
    expected_profit_pct = float(signal.get("expected_profit_pct") or 0)
    cost_ratio = expected_profit_pct / cost_pct if cost_pct > 0 else 999.0
    if cost_ratio < 2.0:
        penalties["cost_drag"] = min(18.0, (2.0 - cost_ratio) * 9.0)
    if regime == "rotation" and float(symbol_context.get("absolute_move_percentile") or 0) >= 0.95:
        penalties["rotation_exhaustion"] = 6.0
    if direction == "SHORT" and regime == "broad_up":
        penalties["countertrend_short"] = 15.0
    if direction == "LONG" and regime in {"broad_down", "panic"}:
        penalties["countertrend_long"] = 15.0

    score = _clamp(sum(components.values()) - sum(penalties.values()), 0.0, 100.0)
    liquidity_safe = depth_known and (
        spread <= float(config.get("opportunity_v3_max_spread_pct", 0.10))
        and depth_notional >= float(config.get("opportunity_v3_min_depth_notional_usdt", 5_000.0))
    )
    direction_floor = float(
        config.get("opportunity_v3_short_strength_floor", 0.68)
        if direction == "SHORT"
        else config.get("opportunity_v3_long_strength_floor", 0.58)
    )
    signal_ready = signal.get("signal") == direction and entry_type in V3_ENTRY_TYPES
    capitulation_short = direction == "SHORT" and float(signal.get("impulse_atr") or 0) >= 2.5 and float(signal.get("adverse_wick_ratio") or 0) >= 1.5
    countertrend = (direction == "SHORT" and regime == "broad_up") or (
        direction == "LONG" and regime in {"broad_down", "panic"}
    )
    panic_chase = (
        regime == "panic"
        and direction == "SHORT"
        and entry_type != "v3_pullback"
        and bool(config.get("opportunity_v3_panic_requires_pullback", True))
    )
    prebreakout_shadow_only = entry_type == "v3_prebreakout" and not bool(
        config.get("opportunity_v3_prebreakout_live_enabled", False)
    )
    extension_atr = float(signal.get("breakout_extension_atr") or 0.0)
    impulse_atr = max(0.0, float(signal.get("impulse_atr") or 0.0))
    overextended = entry_type in {"v3_breakout", "v3_momentum"} and (
        extension_atr > float(config.get("opportunity_v3_max_breakout_extension_atr", 0.65))
        or impulse_atr > float(config.get("opportunity_v3_max_entry_impulse_atr", 1.35))
    )
    weak_medium_path = medium_ready and medium_path_efficiency < float(
        config.get("opportunity_v3_min_medium_path_efficiency", 0.16)
    )
    medium_required = bool(config.get("opportunity_v3_medium_confirmation_enabled", True))
    eligible = (
        signal_ready
        and strength >= direction_floor
        and liquidity_safe
        and not capitulation_short
        and not panic_chase
        and not overextended
        and not weak_medium_path
        and not (countertrend and bool(config.get("opportunity_v3_block_countertrend", True)))
        and not (medium_required and medium_ready and not medium_trend_aligned)
    )
    a_plus_score = float(config.get("opportunity_v3_a_plus_score", 82.0))
    a_score = float(config.get("opportunity_v3_a_score", 70.0))
    b_score = float(config.get("opportunity_v3_b_score", 58.0))
    a_plus_medium_ok = not bool(config.get("opportunity_v3_a_plus_requires_medium_alignment", True)) or (
        medium_ready and medium_trend_aligned
    )
    if eligible and a_plus_medium_ok and score >= a_plus_score and cost_ratio >= float(config.get("opportunity_v3_a_plus_min_cost_ratio", 3.0)):
        tier, label, risk_multiplier, passed = "A+", "顶级机会", 1.0, True
    elif eligible and score >= a_score and cost_ratio >= float(config.get("opportunity_v3_a_min_cost_ratio", 2.0)):
        tier, label, risk_multiplier, passed = "A", "优质机会", 0.65, True
    elif eligible and score >= b_score and cost_ratio >= float(config.get("opportunity_v3_b_min_cost_ratio", 1.35)):
        tier, label, risk_multiplier = "B", "影子观察", 0.25
        passed = bool(config.get("opportunity_v3_b_live_enabled", False))
    else:
        tier, label, risk_multiplier, passed = "WATCH", "继续观察", 0.0, False

    if prebreakout_shadow_only and tier in {"A+", "A", "B"}:
        tier, label, risk_multiplier, passed = "B", "影子观察", 0.25, False

    blockers = []
    if not signal_ready:
        blockers.append("趋势结构尚未触发")
    if strength < direction_floor:
        blockers.append(f"相对强弱分位 {strength:.2f} 低于 {direction_floor:.2f}")
    if not liquidity_safe:
        blockers.append("盘口点差或深度不适合执行")
    if capitulation_short:
        blockers.append("单根急跌后禁止追空")
    if countertrend and bool(config.get("opportunity_v3_block_countertrend", True)):
        blockers.append("方向与全市场趋势相反")
    if panic_chase:
        blockers.append("恐慌行情禁止直接追空，等待回踩确认")
    if prebreakout_shadow_only:
        blockers.append("突破前预判只做影子验证")
    if overextended:
        blockers.append("价格已偏离触发位过远，禁止追单")
    if weak_medium_path:
        blockers.append("中周期路径过于反复")
    if medium_required and medium_ready and not medium_trend_aligned:
        blockers.append("1小时中周期趋势未同向")
    if cost_ratio < float(config.get("opportunity_v3_b_min_cost_ratio", 1.35)):
        blockers.append("扣费后空间不足")
    if tier == "B" and not passed:
        blockers.append("B 级仅进入影子交易")
    reason = (
        f"V3 {label}：{signal.get('entry_type_label')}，相对强弱 {strength * 100:.1f} 分位，"
        f"量能 {volume_acceleration:.2f}x，收益/成本 {cost_ratio:.2f}x"
        if passed
        else "V3 等待：" + "；".join(blockers or ["机会分尚未达到实盘标准"])
    )
    return {
        "enabled": True,
        "engine": "opportunity_v3",
        "strategy_family": V3_STRATEGY_FAMILY,
        "strategy_version": str(config.get("opportunity_v3_strategy_version") or "v3.2"),
        "strategy_role": "active",
        "score": round(score, 4),
        "tier": tier,
        "tier_label": label,
        "passed": passed,
        "eligible": eligible,
        "risk_multiplier": risk_multiplier,
        "canary_eligible": tier == "A+" and eligible and liquidity_safe,
        "reason": reason,
        "blockers": blockers,
        "components": {key: round(value, 4) for key, value in components.items()},
        "penalties": {key: round(value, 4) for key, value in penalties.items()},
        "market_regime": regime,
        "market_regime_label": market_context.get("label"),
        "direction_multiplier": direction_multiplier,
        "strength_percentile": round(strength, 6),
        "cost_ratio": round(cost_ratio, 6),
        "expected_profit_pct": round(expected_profit_pct, 6),
        "estimated_cost_pct": round(cost_pct, 6),
        "event_age_seconds": round(event_age, 3) if event_age is not None else None,
        "liquidity_safe": liquidity_safe,
        "entry_phase": signal.get("entry_phase") or "WATCH",
        "medium_ready": medium_ready,
        "medium_trend_aligned": medium_trend_aligned,
        "medium_path_efficiency": round(medium_path_efficiency, 6),
        "countertrend_blocked": countertrend and bool(config.get("opportunity_v3_block_countertrend", True)),
        "panic_chase_blocked": panic_chase,
        "overextended": overextended,
    }


def build_v31_medium_context(
    bars_by_symbol: dict[str, list[list[Any]]],
) -> dict[str, dict[str, float | bool]]:
    """Build a medium-horizon cross-section from one shared, cached 1h bar set."""
    raw: dict[str, dict[str, float | bool]] = {}
    returns_3d: list[float] = []
    returns_7d: list[float] = []
    for symbol, bars in bars_by_symbol.items():
        if len(bars) < 80:
            continue
        closes = [float(row[4]) for row in bars]
        e20 = ema(closes, 20)
        e50 = ema(closes, 50)
        ret_3d = (closes[-1] / closes[-73] - 1.0) * 100 if len(closes) >= 73 else 0.0
        ret_7d = (closes[-1] / closes[-169] - 1.0) * 100 if len(closes) >= 169 else ret_3d
        path = closes[-73:] if len(closes) >= 73 else closes
        travelled = sum(abs(path[index] - path[index - 1]) for index in range(1, len(path)))
        efficiency = abs(path[-1] - path[0]) / travelled if travelled > 0 else 0.0
        slope = (e20[-1] - e20[-5]) / max(closes[-1], 0.00000001)
        raw[symbol] = {
            "return_3d_pct": ret_3d,
            "return_7d_pct": ret_7d,
            "path_efficiency": _clamp(efficiency, 0.0, 1.0),
            "trend_long": closes[-1] > e20[-1] > e50[-1] and slope > 0,
            "trend_short": closes[-1] < e20[-1] < e50[-1] and slope < 0,
            "ema20_slope": slope,
        }
        returns_3d.append(ret_3d)
        returns_7d.append(ret_7d)
    for item in raw.values():
        item["long_rank_3d"] = _percentile_rank(returns_3d, float(item["return_3d_pct"]))
        item["short_rank_3d"] = 1.0 - float(item["long_rank_3d"])
        item["long_rank_7d"] = _percentile_rank(returns_7d, float(item["return_7d_pct"]))
        item["short_rank_7d"] = 1.0 - float(item["long_rank_7d"])
    return raw


def build_v31_challenger(
    *,
    symbol: str,
    direction: str,
    signal: dict[str, Any],
    opportunity: dict[str, Any],
    medium_context: dict[str, dict[str, float | bool]],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Score V3.1 without making it eligible for live execution."""
    direction = direction.upper()
    medium = medium_context.get(symbol.upper()) or {}
    if not config.get("opportunity_v31_challenger_enabled", True) or not medium:
        return {"enabled": False, "strategy_family": V31_CHALLENGER_FAMILY, "reason": "medium_horizon_not_ready"}
    is_short = direction == "SHORT"
    trend_aligned = bool(medium.get("trend_short" if is_short else "trend_long"))
    return_3d = float(medium.get("return_3d_pct") or 0)
    return_7d = float(medium.get("return_7d_pct") or 0)
    signed_3d = -return_3d if is_short else return_3d
    signed_7d = -return_7d if is_short else return_7d
    rank_3d = float(medium.get("short_rank_3d" if is_short else "long_rank_3d") or 0.5)
    rank_7d = float(medium.get("short_rank_7d" if is_short else "long_rank_7d") or 0.5)
    efficiency = float(medium.get("path_efficiency") or 0)
    adjustment = 0.0
    components = {
        "medium_trend": 10.0 if trend_aligned else -12.0,
        "medium_strength": ((rank_3d + rank_7d) / 2.0 - 0.5) * 16.0,
        "path_quality": (efficiency - 0.25) * 12.0,
    }
    adjustment += sum(components.values())
    blockers: list[str] = []
    if not trend_aligned:
        blockers.append("1小时中周期趋势未同向")
    if max(signed_3d, signed_7d) <= 0:
        blockers.append("3日和7日动量未确认")
    if efficiency < float(config.get("opportunity_v31_min_path_efficiency", 0.18)):
        blockers.append("趋势路径过于反复")
    if not opportunity.get("liquidity_safe"):
        blockers.append("盘口执行质量未通过")
    if signal.get("signal") != direction:
        blockers.append("短周期尚未触发")
    score = _clamp(float(opportunity.get("score") or 0) + adjustment, 0.0, 100.0)
    min_score = float(config.get("opportunity_v31_shadow_min_score", 68.0))
    eligible = not blockers and score >= min_score
    profile = dict(signal.get("protection_profile") or {})
    entry_type = str(signal.get("entry_type") or "watch")
    if entry_type in {"v3_breakout", "v3_pullback"}:
        profile.update({"take_profit_atr": 2.8 if entry_type == "v3_breakout" else 2.4, "max_hold_bars": 24 if entry_type == "v3_breakout" else 18})
    profile["protection_version"] = "v5_dynamic"
    return {
        "enabled": True,
        "strategy_family": V31_CHALLENGER_FAMILY,
        "score": round(score, 4),
        "eligible": eligible,
        "shadow_only": True,
        "reason": "V3.1挑战者通过，进入影子验证" if eligible else "；".join(blockers or [f"挑战者评分 {score:.2f}<{min_score:.2f}"]),
        "blockers": blockers,
        "components": {key: round(value, 4) for key, value in components.items()},
        "medium": {key: round(value, 6) if isinstance(value, float) else value for key, value in medium.items()},
        "protection_profile": profile,
    }


def build_v33_challenger(
    *,
    symbol: str,
    direction: str,
    signal: dict[str, Any],
    opportunity: dict[str, Any],
    medium_context: dict[str, dict[str, float | bool]],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Build the paired, shadow-only V3.3 policy from the shared V3 market snapshot."""
    version = str(config.get("opportunity_v33_strategy_version") or V33_CHALLENGER_VERSION)
    if not config.get("opportunity_v33_challenger_enabled", True):
        return {
            "enabled": False,
            "strategy_family": V3_STRATEGY_FAMILY,
            "strategy_version": version,
            "strategy_role": "challenger",
            "reason": "v33_disabled",
        }

    direction = direction.upper()
    entry_type = str(signal.get("entry_type") or "watch")
    regime = str(opportunity.get("market_regime") or "mixed")
    medium = medium_context.get(symbol.upper()) or {}
    is_short = direction == "SHORT"
    medium_aligned = bool(medium.get("trend_short" if is_short else "trend_long"))
    path_efficiency = float(medium.get("path_efficiency") or 0)
    flow = float(signal.get("directed_trade_flow") or 0)
    volume = float(signal.get("volume_acceleration") or 0)
    cost_ratio = float(opportunity.get("cost_ratio") or 0)
    score = float(opportunity.get("score") or 0)
    signal_ready = signal.get("signal") == direction and entry_type in V3_ENTRY_TYPES

    blockers: list[str] = []
    if not signal_ready:
        blockers.append("短周期结构尚未触发")
    if not opportunity.get("liquidity_safe"):
        blockers.append("盘口执行质量不足")
    if opportunity.get("overextended"):
        blockers.append("价格偏离触发位过远")
    if opportunity.get("countertrend_blocked"):
        blockers.append("方向与全市场趋势相反")

    min_score = float(config.get("opportunity_v33_min_score", 68.0))
    min_cost_ratio = float(config.get("opportunity_v33_min_cost_ratio", 2.5))
    min_path = float(config.get("opportunity_v33_min_medium_path_efficiency", 0.18))
    profile = dict(signal.get("protection_profile") or {})
    components: dict[str, float] = {
        "v32_base_score": score,
        "cost_quality": min(cost_ratio, 5.0) * 1.5,
    }

    if entry_type == "v3_breakout":
        min_score = max(min_score, float(config.get("opportunity_v33_breakout_min_score", 70.0)))
        components["entry_evidence"] = 3.0
        if medium and not medium_aligned:
            blockers.append("中周期趋势未同向")
        if medium and path_efficiency < min_path:
            blockers.append("中周期路径反复")
    elif entry_type == "v3_prebreakout":
        min_score = max(min_score, float(config.get("opportunity_v33_prebreakout_min_score", 68.0)))
        components["entry_evidence"] = 2.0
        if not medium_aligned:
            blockers.append("预突破缺少中周期确认")
        if path_efficiency < min_path:
            blockers.append("预突破路径质量不足")
    elif entry_type == "v3_pullback":
        components["entry_evidence"] = 6.0
        if not medium_aligned:
            blockers.append("回踩与中周期趋势未同向")
        if path_efficiency < min_path:
            blockers.append("回踩趋势路径过于反复")
        if flow < float(config.get("opportunity_v33_pullback_min_flow", 0.50)):
            blockers.append("回踩后的主动成交尚未重新同向")
        if volume < float(config.get("opportunity_v33_pullback_min_volume_acceleration", 0.90)):
            blockers.append("回踩确认量能不足")
        profile.update(
            {
                "take_profit_atr": float(config.get("opportunity_v33_pullback_take_profit_atr", 2.4)),
                "max_hold_bars": int(config.get("opportunity_v33_pullback_max_hold_bars", 18)),
            }
        )
    elif entry_type == "v3_momentum":
        components["entry_evidence"] = -3.0
        min_score = max(min_score, 75.0)
        min_cost_ratio = max(min_cost_ratio, 3.0)
        if not config.get("opportunity_v33_momentum_shadow_enabled", True):
            blockers.append("动量实验已关闭")
        if not medium_aligned or path_efficiency < min_path:
            blockers.append("动量缺少中周期趋势确认")
    else:
        blockers.append("不属于 V3.3 实验入场类型")

    if regime == "panic" and entry_type != "v3_pullback" and config.get("opportunity_v33_panic_pullback_only", True):
        blockers.append("恐慌行情只验证回踩确认，不追涨杀跌")
    if score < min_score:
        blockers.append(f"候选评分 {score:.2f} 低于 {min_score:.2f}")
    if cost_ratio < min_cost_ratio:
        blockers.append(f"预期收益成本比 {cost_ratio:.2f} 低于 {min_cost_ratio:.2f}")

    adjusted_score = _clamp(score + sum(value for key, value in components.items() if key != "v32_base_score"), 0, 100)
    eligible = not blockers
    profile["protection_version"] = "v33-shadow"
    return {
        "enabled": True,
        "strategy_family": V3_STRATEGY_FAMILY,
        "strategy_version": version,
        "strategy_role": "challenger",
        "score": round(adjusted_score, 4),
        "eligible": eligible,
        "shadow_only": True,
        "entry_type": entry_type,
        "market_regime": regime,
        "cost_ratio": round(cost_ratio, 6),
        "medium_ready": bool(medium),
        "medium_trend_aligned": medium_aligned,
        "medium_path_efficiency": round(path_efficiency, 6),
        "reason": "V3.3 配对影子候选通过" if eligible else "；".join(blockers),
        "blockers": blockers,
        "components": {key: round(value, 4) for key, value in components.items()},
        "protection_profile": profile,
    }
