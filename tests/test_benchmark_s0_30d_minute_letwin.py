from __future__ import annotations

from scripts.benchmark_s0_30d_minute_letwin import (
    profiles,
    scenario_passes,
)


def test_profiles_are_preregistered() -> None:
    items = profiles()
    assert {item.name for item in items} == {
        "momentum_30d_letwin_3_5R",
        "momentum_30d_letwin_4R",
    }
    for item in items:
        assert item.stop_atr == 2.5
        assert item.hold_hours == 168


def test_scenario_passes_rejects_negative_year() -> None:
    annual = {str(year): {"profit_factor": 1.2, "net_pct_points": 1.0} for year in range(2021, 2027)}
    annual["2023"] = {"profit_factor": 0.9, "net_pct_points": -1.0}
    result = {
        "overall": {"trades": 70, "profit_factor": 1.5},
        "annual": annual,
        "without_top_3_symbols": {"profit_factor": 1.1, "net_pct_points": 5.0},
        "cohorts": {"blind": {"trades": 10, "profit_factor": 1.2}},
        "bootstrap": {"positive_probability": 0.96},
    }
    assert scenario_passes(result) is False


def test_scenario_passes_accepts_strong_report() -> None:
    annual = {str(year): {"profit_factor": 1.3, "net_pct_points": 2.0} for year in range(2021, 2027)}
    result = {
        "overall": {"trades": 70, "profit_factor": 1.5},
        "annual": annual,
        "without_top_3_symbols": {"profit_factor": 1.1, "net_pct_points": 5.0},
        "cohorts": {"blind": {"trades": 10, "profit_factor": 1.2}},
        "bootstrap": {"positive_probability": 0.96},
    }
    assert scenario_passes(result) is True
