from __future__ import annotations

import math
from typing import Any

from app.binance_rate import cache_get, cache_set
from app.binance_client import BinanceFuturesClient


def _clamp(value: float, lower: float = -1.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _ratio_bias(value: Any) -> float:
    try:
        ratio = max(float(value), 0.000001)
    except (TypeError, ValueError):
        return 0.0
    return _clamp(math.log(ratio) / math.log(2.0))


def _series_change(rows: list[dict[str, Any]], key: str) -> float:
    if len(rows) < 2:
        return 0.0
    try:
        first = max(float(rows[0].get(key) or 0), 0.000001)
        last = max(float(rows[-1].get(key) or 0), 0.000001)
    except (TypeError, ValueError):
        return 0.0
    return _clamp((last / first - 1.0) / 0.08)


def _latest(rows: list[dict[str, Any]], key: str) -> float:
    if not rows:
        return 0.0
    try:
        return float(rows[-1].get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _build_signal(
    symbol: str,
    *,
    position_rows: list[dict[str, Any]],
    account_rows: list[dict[str, Any]],
    taker_rows: list[dict[str, Any]],
    oi_rows: list[dict[str, Any]],
    price_change_pct: float,
    funding_rate_pct: float,
) -> dict[str, Any]:
    position_bias = _ratio_bias(_latest(position_rows, "longShortRatio"))
    position_change = _series_change(position_rows, "longShortRatio")
    account_bias = _ratio_bias(_latest(account_rows, "longShortRatio"))
    taker_bias = _ratio_bias(_latest(taker_rows, "buySellRatio"))
    oi_change = _series_change(oi_rows, "sumOpenInterestValue")
    price_direction = _clamp(price_change_pct / 5.0)
    oi_confirmation = oi_change * (1.0 if price_direction >= 0 else -1.0)
    divergence = abs(account_bias - position_bias)
    divergence_penalty = min(divergence * 0.10, 0.10)

    raw = (
        position_bias * 0.25
        + position_change * 0.20
        + account_bias * 0.10
        + taker_bias * 0.25
        + oi_confirmation * 0.20
    )
    if raw > 0:
        raw -= divergence_penalty
    elif raw < 0:
        raw += divergence_penalty

    funding_crowding = _clamp(funding_rate_pct / 0.10)
    if raw * funding_crowding > 0:
        raw -= math.copysign(min(abs(funding_crowding) * 0.12, 0.12), raw)

    score = _clamp(raw)
    source_count = sum(bool(rows) for rows in (position_rows, account_rows, taker_rows, oi_rows))
    point_coverage = min(
        1.0,
        sum(min(len(rows) / 6.0, 1.0) for rows in (position_rows, account_rows, taker_rows, oi_rows)) / 4.0,
    )
    confidence = source_count / 4.0 * point_coverage
    return {
        "enabled": True,
        "available": any((position_rows, account_rows, taker_rows, oi_rows)),
        "symbol": symbol.upper(),
        "score": round(score, 6),
        "confidence": round(confidence, 6),
        "bias": "LONG" if score > 0.08 else "SHORT" if score < -0.08 else "NEUTRAL",
        "components": {
            "top_position_bias": round(position_bias, 6),
            "top_position_change": round(position_change, 6),
            "top_account_bias": round(account_bias, 6),
            "aggressive_taker_flow": round(taker_bias, 6),
            "open_interest_confirmation": round(oi_confirmation, 6),
            "crowding_penalty": round(divergence_penalty, 6),
        },
        "sample_points": {
            "top_position": len(position_rows),
            "top_account": len(account_rows),
            "taker": len(taker_rows),
            "open_interest": len(oi_rows),
        },
    }


def smart_flow_signal(
    client: BinanceFuturesClient,
    symbol: str,
    config: dict[str, Any],
    *,
    price_change_pct: float = 0.0,
    funding_rate_pct: float = 0.0,
    allow_fetch: bool = True,
) -> dict[str, Any]:
    symbol = symbol.upper()
    cache_key = f"smart_flow_composite:{symbol}"
    ttl = int(config.get("smart_flow_cache_seconds", 300))
    cached = cache_get(cache_key, ttl)
    if cached:
        return dict(cached.value)
    if not allow_fetch:
        return {
            "enabled": True,
            "available": False,
            "symbol": symbol,
            "score": 0.0,
            "bias": "NEUTRAL",
            "reason": "awaiting_background_refresh",
        }

    period = str(config.get("smart_flow_period", "5m"))
    limit = int(config.get("smart_flow_history_points", 12))
    try:
        signal = _build_signal(
            symbol,
            position_rows=client.top_trader_position_ratio(symbol, period, limit),
            account_rows=client.top_trader_account_ratio(symbol, period, limit),
            taker_rows=client.taker_buy_sell_ratio(symbol, period, limit),
            oi_rows=client.open_interest_hist(symbol, period, limit),
            price_change_pct=price_change_pct,
            funding_rate_pct=funding_rate_pct,
        )
    except Exception as exc:
        signal = {
            "enabled": True,
            "available": False,
            "symbol": symbol,
            "score": 0.0,
            "bias": "NEUTRAL",
            "reason": str(exc),
        }
    cache_set(cache_key, signal)
    return signal


def enrich_smart_flow_candidates(
    client: BinanceFuturesClient,
    candidates: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    fast_lane: bool = False,
) -> list[dict[str, Any]]:
    if not config.get("smart_flow_enabled", True):
        return candidates
    limit = int(config.get("smart_flow_symbol_limit", 12))
    ranked = sorted(candidates, key=lambda item: float(item.get("score") or -999), reverse=True)
    selected = {str(item.get("symbol") or "").upper() for item in ranked[:limit]}
    max_points = float(config.get("smart_flow_max_soft_points", 5.0))
    live_enabled = bool(config.get("smart_flow_live_soft_score_enabled", False))
    for candidate in candidates:
        symbol = str(candidate.get("symbol") or "").upper()
        if not symbol or symbol not in selected:
            candidate["smart_flow"] = {"enabled": True, "available": False, "score": 0.0, "bias": "NEUTRAL"}
            candidate["smart_flow_score_delta"] = 0.0
            continue
        derivatives = candidate.get("derivatives") or {}
        signal = smart_flow_signal(
            client,
            symbol,
            config,
            price_change_pct=float((candidate.get("ticker") or {}).get("change_pct") or 0),
            funding_rate_pct=float(derivatives.get("funding_rate_pct") or 0),
            allow_fetch=not fast_lane,
        )
        direction = str(candidate.get("direction") or "").upper()
        directional_score = float(signal.get("score") or 0) * (1.0 if direction == "LONG" else -1.0)
        confidence = float(signal.get("confidence") or 0)
        minimum_confidence = float(config.get("smart_flow_min_confidence", 0.45))
        delta = (
            _clamp(directional_score) * max_points
            if live_enabled and confidence >= minimum_confidence
            else 0.0
        )
        candidate["smart_flow"] = {
            **signal,
            "directional_alignment": round(directional_score, 6),
            "live_soft_score_enabled": live_enabled,
            "minimum_confidence": minimum_confidence,
        }
        candidate["smart_flow_score_delta"] = round(delta, 6)
    return candidates
