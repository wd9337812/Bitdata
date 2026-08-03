from scripts.audit_s0_30d_fast_exit_cross_year import (
    REQUIRED_YEARS,
    frozen_profile,
    scenario_passes,
)


def passing_scenario() -> dict:
    return {
        "overall": {"trades": 80, "profit_factor": 1.4},
        "annual": {
            year: {"profit_factor": 1.1, "net_pct_points": 1.0}
            for year in REQUIRED_YEARS
        },
        "without_top_3_symbols": {
            "profit_factor": 1.1,
            "net_pct_points": 2.0,
        },
        "cohorts": {"blind": {"trades": 10, "profit_factor": 1.1}},
        "bootstrap": {"positive_probability": 0.97},
    }


def test_frozen_profile_is_the_registered_fast_exit() -> None:
    profile = frozen_profile()

    assert profile.stop_atr == 2.0
    assert profile.reward_r == 1.5
    assert profile.hold_hours == 72
    assert profile.max_stop_pct == 10.0


def test_scenario_requires_positive_diversified_evidence() -> None:
    scenario = passing_scenario()
    assert scenario_passes(scenario)

    scenario["without_top_3_symbols"]["net_pct_points"] = -0.01
    assert not scenario_passes(scenario)


def test_scenario_rejects_a_single_negative_year() -> None:
    scenario = passing_scenario()
    scenario["annual"]["2023"]["profit_factor"] = 0.99

    assert not scenario_passes(scenario)
