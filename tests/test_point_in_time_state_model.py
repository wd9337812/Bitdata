from __future__ import annotations

import pandas as pd

from scripts.benchmark_s0_point_in_time_state_model import (
    choose_validation_threshold,
    prediction_signals,
    purged_window,
)


def test_purged_window_excludes_label_crossing_boundary() -> None:
    end = pd.Timestamp("2026-04-01", tz="UTC")
    frame = pd.DataFrame(
        {
            "available_ms": [
                int((end - pd.Timedelta(hours=5)).timestamp() * 1000),
                int((end - pd.Timedelta(hours=1)).timestamp() * 1000),
            ],
            "label_exit_ms": [
                int((end - pd.Timedelta(hours=1)).timestamp() * 1000),
                int((end + pd.Timedelta(hours=3)).timestamp() * 1000),
            ],
        }
    )
    result = purged_window(frame, "2026-01-01", "2026-04-01")
    assert len(result) == 1


def test_prediction_auction_keeps_largest_absolute_edge() -> None:
    frame = pd.DataFrame(
        {
            "available_ms": [1, 1, 2],
            "symbol": ["A", "B", "C"],
            "predicted_return": [0.02, -0.03, 0.001],
            "liquidity_24h": [10, 20, 10],
        }
    )
    result = prediction_signals(frame, 0.01)
    assert result.symbol.tolist() == ["B"]
    assert result.direction.tolist() == ["SHORT"]


def test_validation_threshold_requires_cost_stress_profit() -> None:
    reports = {
        "q900": {
            "base": {
                "trades": 100,
                "profit_factor": 1.20,
                "net_pct_points": 10,
            },
            "stress": {
                "trades": 100,
                "profit_factor": 0.99,
                "net_pct_points": -1,
            },
        },
        "q950": {
            "base": {
                "trades": 50,
                "profit_factor": 1.10,
                "net_pct_points": 8,
            },
            "stress": {
                "trades": 50,
                "profit_factor": 1.03,
                "net_pct_points": 2,
            },
        },
    }
    assert choose_validation_threshold(reports) == "q950"
    assert choose_validation_threshold(reports, minimum_trades=60) is None
