from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from statistics import median
from typing import Any

from app.strategy import atr, ema


V3_STRATEGY_FAMILY = "extreme_v3_roll"
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
    for lookback in lookbacks:
        if len(bars) <= lookback:
            continue
        upper = max(highs[-lookback - 1 : -1])
        lower = min(lows[-lookback - 1 : -1])
        if is_short:
            votes += int(close <= lower)
            distances.append(max(0.0, close - lower) / atr_value)
        else:
            votes += int(close >= upper)
            distances.append(max(0.0, upper - close) / atr_value)
    distance_atr = min(distances) if distances else 999.0
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
    return {
        "enabled": True,
        "engine": "opportunity_v3",
        "strategy_family": V3_STRATEGY_FAMILY,
        "symbol": symbol,
        "signal": direction if signal_ready else "WAIT",
        "direction": direction,
        "reason": setup_type if signal_ready else "v3_waiting_for_structure",
        "strategy": "opportunity_v3_trend",
        "entry_type": setup_type,
        "entry_type_label": label,
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
    depth: dict[str, Any],
    derivatives: dict[str, Any],
    event: dict[str, Any] | None,
    cost_pct: float,
    config: dict[str, Any],
) -> dict[str, Any]:
    direction = direction.upper()
    symbol_context = (market_context.get("symbols") or {}).get(symbol.upper(), {})
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
        liquidity = 3.5

    components = {
        "relative_strength": relative,
        "trend_structure": trend,
        "volume_flow": activity,
        "breakout_quality": breakout,
        "regime_fit": regime_component,
        "derivatives": derivative_component,
        "liquidity": liquidity,
    }
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
    liquidity_safe = not depth_known or (
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
    eligible = signal_ready and strength >= direction_floor and liquidity_safe and not capitulation_short
    a_plus_score = float(config.get("opportunity_v3_a_plus_score", 82.0))
    a_score = float(config.get("opportunity_v3_a_score", 70.0))
    b_score = float(config.get("opportunity_v3_b_score", 58.0))
    if eligible and score >= a_plus_score and cost_ratio >= float(config.get("opportunity_v3_a_plus_min_cost_ratio", 3.0)):
        tier, label, risk_multiplier, passed = "A+", "顶级机会", 1.0, True
    elif eligible and score >= a_score and cost_ratio >= float(config.get("opportunity_v3_a_min_cost_ratio", 2.0)):
        tier, label, risk_multiplier, passed = "A", "优质机会", 0.65, True
    elif eligible and score >= b_score and cost_ratio >= float(config.get("opportunity_v3_b_min_cost_ratio", 1.35)):
        tier, label, risk_multiplier = "B", "影子观察", 0.25
        passed = bool(config.get("opportunity_v3_b_live_enabled", False))
    else:
        tier, label, risk_multiplier, passed = "WATCH", "继续观察", 0.0, False

    blockers = []
    if not signal_ready:
        blockers.append("趋势结构尚未触发")
    if strength < direction_floor:
        blockers.append(f"相对强弱分位 {strength:.2f} 低于 {direction_floor:.2f}")
    if not liquidity_safe:
        blockers.append("盘口点差或深度不适合执行")
    if capitulation_short:
        blockers.append("单根急跌后禁止追空")
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
    }
