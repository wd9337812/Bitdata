from scripts.audit_s0_new_listing_momentum_long import (
    PROFILES,
    selection_score,
)


def test_profiles_are_frozen_before_evaluation():
    names = [profile.name for profile in PROFILES]

    assert "time_only" in names
    assert "momentum_stop10_tp20" in names
    assert "momentum_stop15_tp30" in names
    assert len(PROFILES) == 5


def test_selection_uses_development_only():
    report = {
        "development_2020_2022": {
            "stress_cost": {
                "net_return": 3.0,
                "profit_factor": 2.0,
            }
        },
        "validation_2023": {"stress_cost": {"net_return": -5.0}},
        "test_2024": {"stress_cost": {"net_return": 10.0}},
        "blind_2025": {"stress_cost": {"net_return": 20.0}},
        "final_2026": {"stress_cost": {"net_return": 30.0}},
    }

    score = selection_score(report)

    # The score must not include test/blind/final windows (no look-ahead).
    assert score == (1, 3.0, 2.0, 3.0)
