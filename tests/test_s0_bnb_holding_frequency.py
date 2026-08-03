from scripts.benchmark_s0_bnb_holding_frequency import PROFILES, selection_score


def _report(annual: list[float], profit_factor: float, total_return: float) -> dict:
    return {
        "annual": {
            str(index): {"return_pct": value}
            for index, value in enumerate(annual)
        },
        "overall": {
            "profit_factor": profit_factor,
            "return_pct": total_return,
        },
    }


def test_frequency_selection_prefers_cross_year_robustness() -> None:
    robust = _report([10.0, 1.0, 5.0], 1.2, 20.0)
    fragile = _report([30.0, -0.1, 40.0], 2.5, 100.0)

    assert selection_score(robust) > selection_score(fragile)


def test_frequency_grid_contains_live_and_challenger_profiles() -> None:
    profiles = {profile.name for profile in PROFILES}

    assert "time5_stop10" in profiles
    assert "time3_stop15" in profiles
