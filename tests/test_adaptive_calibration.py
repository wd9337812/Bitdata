from __future__ import annotations

from app import adaptive_calibration as module


def _candidate(direction: str, regime: str = "broad_up", setup: str = "momentum") -> dict:
    return {
        "direction": direction,
        "entry_type": setup,
        "market_structure": {
            "market_regime": regime,
            "medium_trend_aligned": (regime == "broad_up" and direction == "LONG")
            or (regime == "broad_down" and direction == "SHORT"),
            "setup_type": setup,
        },
    }


def _row(direction: str, net_pct: float, *, age: float = 2, version: str = "v4.7", source: str = "shadow", symbol: str | None = None, regime: str | None = None) -> dict:
    return {
        "source": source,
        "version": version,
        "symbol": symbol or f"ALT{abs(hash((direction, net_pct, age, source))) % 1000}USDT",
        "direction": direction,
        "market_regime": regime or ("broad_up" if direction == "LONG" else "broad_down"),
        "setup_type": "momentum",
        "age_hours": age,
        "net_pct": net_pct,
        "opportunity_id": f"{direction}:{net_pct}:{age}",
    }


def test_v47_uses_symmetric_market_relation_priors(monkeypatch):
    monkeypatch.setattr(module, "calibration_rows", lambda config: [])
    config = {"opportunity_v4_strategy_version": "v4.7"}

    aligned_long = module.adaptive_calibration(_candidate("LONG", "broad_up"), config)
    counter_short = module.adaptive_calibration(_candidate("SHORT", "broad_up"), config)
    aligned_short = module.adaptive_calibration(_candidate("SHORT", "broad_down"), config)

    assert aligned_long["risk_multiplier"] == 1.0
    assert aligned_short["risk_multiplier"] == 1.0
    assert counter_short["risk_multiplier"] == 0.65


def test_v47_current_positive_evidence_can_raise_both_directions(monkeypatch):
    rows = [_row("LONG", 0.30, age=index / 2) for index in range(16)]
    rows += [_row("SHORT", 0.30, age=index / 2) for index in range(16)]
    monkeypatch.setattr(module, "calibration_rows", lambda config: rows)
    config = {"opportunity_v4_strategy_version": "v4.7"}

    long_result = module.adaptive_calibration(_candidate("LONG", "broad_up"), config)
    short_result = module.adaptive_calibration(_candidate("SHORT", "broad_down"), config)

    assert long_result["risk_multiplier"] > 1.0
    assert short_result["risk_multiplier"] > 1.0
    assert long_result["rank_threshold_delta"] == -0.02
    assert short_result["confirmation_delta"] == -1


def test_v47_negative_evidence_reduces_but_respects_floor(monkeypatch):
    rows = [_row("LONG", -0.30, age=index / 2) for index in range(20)]
    monkeypatch.setattr(module, "calibration_rows", lambda config: rows)
    result = module.adaptive_calibration(
        _candidate("LONG", "broad_up"),
        {"opportunity_v4_strategy_version": "v4.7", "opportunity_v47_min_multiplier": 0.95},
    )

    assert result["risk_multiplier"] == 0.95
    assert result["rank_threshold_delta"] == 0.02
    assert result["confirmation_delta"] == 1


def test_v47_seed_evidence_is_shrunk_and_cannot_open_thresholds(monkeypatch):
    rows = [_row("LONG", 1.0, version="v4.6.2") for _ in range(30)]
    monkeypatch.setattr(module, "calibration_rows", lambda config: rows)
    result = module.adaptive_calibration(
        _candidate("LONG", "broad_up"),
        {"opportunity_v4_strategy_version": "v4.7"},
    )

    assert result["risk_multiplier"] == 1.0
    assert result["threshold_adjustment_ready"] is False
    assert result["rank_threshold_delta"] == 0.0


def test_v48_reports_v47_only_as_capped_seed(monkeypatch):
    rows = [_row("LONG", 1.0, version="v4.7") for _ in range(30)]
    monkeypatch.setattr(module, "calibration_rows", lambda config: rows)

    result = module.adaptive_calibration(
        _candidate("LONG", "broad_up"),
        {"opportunity_v4_strategy_version": "v4.8", "opportunity_v48_seed_version": "v4.7"},
    )

    assert result["schema"] == "adaptive_v48"
    assert result["seed_version"] == "v4.7"
    assert result["stats_24h"]["current_trades"] == 0
    assert result["threshold_adjustment_ready"] is False


def test_v49_uses_one_global_current_version_calibration(monkeypatch):
    rows = []
    for index in range(20):
        rows.append(_row("LONG", 0.30, version="v4.9", source="shadow", age=index / 2, symbol=f"ALT{index % 3}USDT", regime="broad_up" if index % 2 else "rotation"))
    for index in range(8):
        rows.append(_row("SHORT", 0.30, version="v4.9", source="live", age=index / 2, symbol=f"ALT{index % 3}USDT", regime="broad_down" if index % 2 else "rotation"))
    rows.append(_row("LONG", 9.0, version="v4.8", source="shadow"))
    monkeypatch.setattr(module, "calibration_rows", lambda config: rows)
    module.clear_adaptive_calibration_cache()
    config = {"opportunity_v4_strategy_version": "v4.9", "opportunity_v49_global_target_live_trades": 12}

    long_result = module.adaptive_calibration(_candidate("LONG", "broad_up"), config)
    short_result = module.adaptive_calibration(_candidate("SHORT", "broad_down"), config)

    assert long_result["schema"] == "adaptive_v49_global"
    assert long_result["scope"] == "current_version_global_24h"
    assert long_result["stats_24h"]["live_trades"] == 8
    assert long_result["stats_24h"]["shadow_trades"] == 20
    assert long_result["rank_threshold_delta"] < 0
    assert short_result["rank_threshold_delta"] == long_result["rank_threshold_delta"]
    assert short_result["risk_multiplier"] == long_result["risk_multiplier"]


def test_v410_adds_global_regime_direction_and_smart_context(monkeypatch):
    rows = []
    for index in range(20):
        row = _row("LONG", 0.25, version="v4.10", source="shadow", age=index / 2, symbol=f"ALT{index % 3}USDT", regime="broad_up")
        row["smart_score"] = 0.25
        rows.append(row)
    for index in range(8):
        row = _row("SHORT", -0.10, version="v4.10", source="live", age=index / 2, symbol=f"ALT{index % 3}USDT", regime="broad_up")
        row["smart_score"] = 0.10
        rows.append(row)
    monkeypatch.setattr(module, "calibration_rows", lambda config: rows)
    module.clear_adaptive_calibration_cache()

    result = module.adaptive_calibration(
        _candidate("LONG", "broad_up"),
        {"opportunity_v4_strategy_version": "v4.10", "opportunity_v49_global_target_live_trades": 12},
    )

    assert result["schema"] == "adaptive_v410_global"
    assert result["market_regime_state"] == "trend"
    assert result["global_direction_bias"] == "LONG"
    assert result["smart_flow_global"]["available"] is True
    assert result["risk_multiplier"] > 1.0
