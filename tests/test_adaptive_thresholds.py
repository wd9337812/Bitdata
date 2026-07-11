from app.adaptive_thresholds import adaptive_thresholds, build_market_profile


def _bars(volumes: list[float]) -> list[list[float]]:
    return [[index, 100, 101, 99, 100, volume / 100, index + 1, 0, 0, 0, 0, 0] for index, volume in enumerate(volumes)]


def test_market_profile_classifies_quiet_and_hot_markets():
    quiet = {f"Q{i}": {"priceChangePercent": value, "quoteVolume": 1_000_000} for i, value in enumerate([0.5, 1, 1.5, 2])}
    hot = {f"H{i}": {"priceChangePercent": value, "quoteVolume": 1_000_000} for i, value in enumerate([4, 6, 9, 15])}

    assert build_market_profile(quiet)["regime"] == "quiet"
    assert build_market_profile(hot)["regime"] == "hot"


def test_adaptive_thresholds_remain_inside_safety_bounds():
    config = {
        "adaptive_thresholds_enabled": True,
        "volume_spike_ratio": 1.8,
        "extreme_firecracker_min_abs_change_pct": 8,
        "adaptive_volume_spike_floor": 1.2,
        "adaptive_volume_spike_ceiling": 2.4,
        "adaptive_firecracker_move_floor_pct": 4,
        "adaptive_firecracker_move_ceiling_pct": 14,
    }
    result = adaptive_thresholds(
        _bars([100, 110, 95, 120, 105] * 20),
        config,
        {"regime": "quiet", "p75_abs_change_pct": 1.0},
    )

    assert 1.2 <= result["volume_spike_min"] <= 2.4
    assert 4 <= result["firecracker_move_min_pct"] <= 14
