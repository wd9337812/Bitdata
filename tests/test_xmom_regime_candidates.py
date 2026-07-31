from __future__ import annotations

import pandas as pd

from scripts.audit_s0_xmom_regime_candidates import (
    candidate_filters,
    choose_frozen_candidate,
    directional_positive,
)


def test_directional_positive_handles_long_and_short() -> None:
    frame = pd.DataFrame(
        {
            "direction": ["LONG", "SHORT", "LONG", "SHORT"],
            "ret_6h": [0.1, -0.1, -0.1, 0.1],
        }
    )
    assert directional_positive(frame, "ret_6h").tolist() == [
        True,
        True,
        False,
        False,
    ]


def test_candidate_family_is_small_and_contains_baseline() -> None:
    filters = candidate_filters()
    assert "baseline" in filters
    assert len(filters) <= 12


def test_frozen_candidate_only_uses_development_report() -> None:
    reports = {
        "a": {
            "development_qualified": True,
            "development": {
                "stress": {
                    "profit_factor": 1.10,
                    "net_pct_points": 5.0,
                },
                "base": {"trades": 50},
            },
            "future_result_that_must_be_ignored": 999,
        },
        "b": {
            "development_qualified": True,
            "development": {
                "stress": {
                    "profit_factor": 1.20,
                    "net_pct_points": 4.0,
                },
                "base": {"trades": 40},
            },
            "future_result_that_must_be_ignored": -999,
        },
    }
    assert choose_frozen_candidate(reports) == "b"


def test_no_candidate_when_development_gate_fails() -> None:
    reports = {
        "baseline": {
            "development_qualified": False,
            "development": {
                "stress": {"profit_factor": 0.9, "net_pct_points": -1},
                "base": {"trades": 100},
            },
        }
    }
    assert choose_frozen_candidate(reports) is None
