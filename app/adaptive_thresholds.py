from __future__ import annotations

import math
from typing import Any


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


def _percentile(values: list[float], percentile: float) -> float:
    clean = sorted(value for value in values if math.isfinite(value))
    if not clean:
        return 0.0
    position = (len(clean) - 1) * _clamp(percentile, 0.0, 1.0)
    lower = int(position)
    upper = min(lower + 1, len(clean) - 1)
    weight = position - lower
    return clean[lower] * (1 - weight) + clean[upper] * weight


def build_market_profile(tickers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Build one cheap cross-sectional baseline from the already-fetched 24h tickers."""
    moves = [
        abs(float(item.get("priceChangePercent") or 0))
        for item in tickers.values()
        if float(item.get("quoteVolume") or 0) > 0
    ]
    median_move = _percentile(moves, 0.50)
    p75_move = _percentile(moves, 0.75)
    p90_move = _percentile(moves, 0.90)
    if median_move < 2.0 and p75_move < 4.0:
        regime = "quiet"
        label = "安静市场"
    elif median_move >= 5.0 or p75_move >= 9.0:
        regime = "hot"
        label = "高波动市场"
    else:
        regime = "normal"
        label = "常态市场"
    return {
        "regime": regime,
        "label": label,
        "sample_size": len(moves),
        "median_abs_change_pct": round(median_move, 4),
        "p75_abs_change_pct": round(p75_move, 4),
        "p90_abs_change_pct": round(p90_move, 4),
    }


def adaptive_thresholds(
    bars: list[list[Any]],
    config: dict[str, Any],
    market_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return bounded thresholds without making any exchange request."""
    enabled = bool(config.get("adaptive_thresholds_enabled", True))
    fixed_volume = float(config.get("volume_spike_ratio", 1.8))
    fixed_move = float(config.get("extreme_firecracker_min_abs_change_pct", 8.0))
    profile = market_profile or config.get("_adaptive_market_profile") or {}
    regime = str(profile.get("regime") or "normal")
    if not enabled:
        return {
            "enabled": False,
            "regime": regime,
            "volume_spike_min": fixed_volume,
            "firecracker_move_min_pct": fixed_move,
        }

    quote_volumes = [
        max(0.0, float(row[5] or 0) * float(row[4] or 0))
        for row in bars[-81:-1]
        if len(row) > 5
    ]
    median_volume = _percentile(quote_volumes, 0.50)
    p70_volume = _percentile(quote_volumes, 0.70)
    symbol_volume_ratio = p70_volume / median_volume if median_volume > 0 else fixed_volume
    regime_volume_factor = {"quiet": 0.88, "normal": 1.0, "hot": 1.12}.get(regime, 1.0)
    volume_min = _clamp(
        max(fixed_volume * 0.70, symbol_volume_ratio) * regime_volume_factor,
        float(config.get("adaptive_volume_spike_floor", 1.20)),
        float(config.get("adaptive_volume_spike_ceiling", 2.40)),
    )

    market_p75 = float(profile.get("p75_abs_change_pct") or fixed_move)
    regime_move_factor = {"quiet": 0.78, "normal": 0.95, "hot": 1.12}.get(regime, 1.0)
    move_min = _clamp(
        max(fixed_move * 0.65, market_p75 * regime_move_factor),
        float(config.get("adaptive_firecracker_move_floor_pct", 4.0)),
        float(config.get("adaptive_firecracker_move_ceiling_pct", 14.0)),
    )
    return {
        "enabled": True,
        "regime": regime,
        "regime_label": profile.get("label", "常态市场"),
        "volume_spike_min": round(volume_min, 4),
        "firecracker_move_min_pct": round(move_min, 4),
        "symbol_volume_p70_ratio": round(symbol_volume_ratio, 4),
        "market_p75_abs_change_pct": round(market_p75, 4),
        "bounds": {
            "volume": [
                float(config.get("adaptive_volume_spike_floor", 1.20)),
                float(config.get("adaptive_volume_spike_ceiling", 2.40)),
            ],
            "move_pct": [
                float(config.get("adaptive_firecracker_move_floor_pct", 4.0)),
                float(config.get("adaptive_firecracker_move_ceiling_pct", 14.0)),
            ],
        },
    }
