from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.benchmark_s0_quarter_hour_order_flow import (
    Variant,
    add_historical_zscore,
    normalized_order_imbalance,
    qualifies,
    select_non_overlapping,
    trade_metrics,
)


def test_normalized_order_imbalance_uses_taker_share() -> None:
    result = normalized_order_imbalance(
        pd.Series([75.0, 25.0, 10.0]),
        pd.Series([100.0, 100.0, 0.0]),
    )
    assert result.iloc[0] == 0.5
    assert result.iloc[1] == -0.5
    assert np.isnan(result.iloc[2])


def test_historical_zscore_does_not_include_current_observation(monkeypatch) -> None:
    import scripts.benchmark_s0_quarter_hour_order_flow as module

    monkeypatch.setattr(module, "ROLLING_QUARTERS", 2)
    monkeypatch.setattr(module, "MIN_HISTORY_QUARTERS", 2)
    frame = pd.DataFrame({"order_imbalance": [0.0, 2.0, 100.0]})
    result = add_historical_zscore(frame)
    assert result.oi_zscore.iloc[2] == 99.0


def test_single_position_selection_is_non_overlapping() -> None:
    times = pd.date_range("2026-01-01", periods=5, freq="15min", tz="UTC")
    panel = pd.DataFrame(
        {
            "time": list(times) * 2,
            "symbol": ["A"] * 5 + ["B"] * 5,
            "order_imbalance": [0.2] * 10,
            "oi_zscore": [2.0] * 5 + [1.0] * 5,
            "previous_quarter": [True] * 10,
            "previous_hour": [True] * 10,
            "gross_forward_60": [0.01] * 10,
        }
    )
    trades = select_non_overlapping(panel, Variant(60, 0.0, "none"))
    assert list(trades.symbol) == ["A", "A"]
    assert list(trades.time) == [times[0], times[4]]


def test_trade_metrics_deduct_round_trip_cost() -> None:
    trades = pd.DataFrame(
        {
            "symbol": ["A", "B"],
            "gross_return": [0.01, -0.005],
        }
    )
    metrics = trade_metrics(trades, 0.001)
    assert metrics["trades"] == 2
    assert metrics["net_pct_points"] == 0.3
    assert metrics["mean_gross_bps"] == 25.0
    assert metrics["mean_net_bps"] == 15.0


def test_qualification_requires_both_oos_windows_and_cost_cases() -> None:
    passing = {"trades": 25, "profit_factor": 1.1, "net_pct_points": 1.0}
    windows = {
        "validation_apr_may": {"base": passing.copy(), "stress": passing.copy()},
        "test_jun_jul": {"base": passing.copy(), "stress": passing.copy()},
    }
    assert qualifies(windows)
    windows["test_jun_jul"]["stress"]["net_pct_points"] = -0.1
    assert not qualifies(windows)
