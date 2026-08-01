from scripts.benchmark_s0_short_momentum_cross_year import (
    PROFILE_NAMES,
    qualifies,
    selected_profiles,
)


def test_selected_profiles_are_frozen_short_momentum_rules() -> None:
    assert tuple(profile.name for profile in selected_profiles()) == PROFILE_NAMES


def test_qualification_requires_each_calendar_year() -> None:
    stress = {
        "funded_symbol_embargo": {
            "overall": {"trades": 120, "symbols": 30, "profit_factor": 1.4},
            "annual": {
                str(year): {"net_pct_points": 1.0, "profit_factor": 1.1}
                for year in range(2021, 2026)
            },
            "without_top_3_symbols": {
                "net_pct_points": 1.0,
                "profit_factor": 1.1,
            },
            "bootstrap": {"positive_probability": 0.97},
        }
    }

    assert not qualifies(stress)


def test_qualification_accepts_diversified_cross_year_candidate() -> None:
    stress = {
        "funded_symbol_embargo": {
            "overall": {
                "trades": 180,
                "symbols": 45,
                "profit_factor": 1.35,
            },
            "annual": {
                str(year): {"net_pct_points": 2.0, "profit_factor": 1.08}
                for year in range(2021, 2027)
            },
            "without_top_3_symbols": {
                "net_pct_points": 4.0,
                "profit_factor": 1.12,
            },
            "bootstrap": {"positive_probability": 0.96},
        }
    }

    assert qualifies(stress)


def test_qualification_rejects_top_symbol_dependence() -> None:
    stress = {
        "funded_symbol_embargo": {
            "overall": {
                "trades": 180,
                "symbols": 45,
                "profit_factor": 1.35,
            },
            "annual": {
                str(year): {"net_pct_points": 2.0, "profit_factor": 1.08}
                for year in range(2021, 2027)
            },
            "without_top_3_symbols": {
                "net_pct_points": -1.0,
                "profit_factor": 0.98,
            },
            "bootstrap": {"positive_probability": 0.96},
        }
    }

    assert not qualifies(stress)
