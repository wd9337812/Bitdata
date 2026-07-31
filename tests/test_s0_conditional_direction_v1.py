from __future__ import annotations

import pandas as pd

from scripts.train_s0_conditional_direction_v1 import (
    HORIZONS,
    MODEL_VERSION,
    calibration_folds,
    lane_mask,
    metrics,
)


def test_model_is_an_isolated_research_version():
    assert MODEL_VERSION == "s0_conditional_direction_v1"
    assert list(HORIZONS) == ["3m", "5m", "10m"]


def test_lane_mask_scopes_direction_and_regime():
    frame = pd.DataFrame(
        {
            "direction": ["LONG", "LONG", "SHORT"],
            "market_regime": ["broad_up", "quiet", "broad_up"],
            "setup_type": ["pullback", "pullback", "momentum"],
        }
    )

    assert lane_mask(frame, "LONG|regime|broad_up").tolist() == [True, False, False]
    assert lane_mask(frame, "SHORT|all|all").tolist() == [False, False, True]


def test_metrics_include_stressed_cost():
    frame = pd.DataFrame(
        {
            "selected_net_pct": [0.2, -0.1],
            "symbol": ["AUSDT", "BUSDT"],
            "market_regime": ["broad_up", "broad_up"],
            "selected_horizon": ["1m", "5m"],
        }
    )

    base = metrics(frame)
    stressed = metrics(frame, extra_cost_pct=0.06)

    assert base["net_pct_points"] == 0.1
    assert stressed["net_pct_points"] == -0.02


def test_calibration_folds_are_chronological_and_disjoint():
    frame = pd.DataFrame(
        {
            "time": pd.date_range("2026-01-01", periods=12, freq="h", tz="UTC"),
        }
    )

    folds = calibration_folds(frame)

    assert sum(len(fold) for fold in folds) == len(frame)
    assert folds[0].time.max() < folds[1].time.min()
    assert folds[1].time.max() < folds[2].time.min()
