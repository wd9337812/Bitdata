from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts.benchmark_s0_btc_cost_aware_lgbm import (
    build_features,
    cost_aware_positions,
    fold_boundaries,
    yearly_metrics,
)


def test_cost_filter_keeps_position_for_weak_flip() -> None:
    predictions = np.array([0.003, -0.0001, -0.0041])
    positions = cost_aware_positions(
        predictions,
        mode="LONG_SHORT",
        one_way_cost=0.001,
        threshold_multiplier=2.0,
    )
    assert positions.tolist() == [1.0, 1.0, -1.0]


def test_target_is_next_open_to_open_after_signal_bar() -> None:
    rows = 400
    index = np.arange(rows, dtype=float)
    opens = 100.0 + index * 0.1 + np.sin(index / 3.0)
    frame = pd.DataFrame(
        {
            "open_time": np.arange(rows) * 3_600_000,
            "open": opens,
            "high": opens + 1,
            "low": opens - 1,
            "close": opens + np.sin(index / 5.0) * 0.5,
            "quote_volume": 1_000_000.0 + (index % 17) * 10_000,
            "taker_buy_quote_volume": 500_000.0 + (index % 11) * 5_000,
            "time": pd.date_range("2024-01-01", periods=rows, freq="h", tz="UTC"),
        }
    )
    featured, _ = build_features(frame)
    row = featured.iloc[0]
    original_index = row.name
    expected = frame.loc[original_index + 2, "open"] / frame.loc[original_index + 1, "open"] - 1
    assert row.target == expected


def test_fold_boundaries_begin_after_twelve_month_train_and_three_month_validation() -> None:
    frame = pd.DataFrame(
        {
            "time": pd.date_range(
                "2020-01-01", "2022-01-01", freq="h", inclusive="left", tz="UTC"
            )
        }
    )
    folds = fold_boundaries(frame)
    assert folds[0]["train_start"] == pd.Timestamp("2020-01-01", tz="UTC")
    assert folds[0]["validation_start"] == pd.Timestamp("2021-01-01", tz="UTC")
    assert folds[0]["test_start"] == pd.Timestamp("2021-04-01", tz="UTC")


def test_yearly_metrics_keep_calendar_periods_separate() -> None:
    returns = pd.DataFrame(
        {
            "time": pd.to_datetime(
                ["2024-12-31 23:00:00Z", "2025-01-01 00:00:00Z"]
            ),
            "turnover": [1.0, 1.0],
            "net_return": [0.10, -0.10],
        }
    )

    result = yearly_metrics(returns)

    assert result["2024"]["net_return"] == pytest.approx(0.10)
    assert result["2025"]["net_return"] == pytest.approx(-0.10)
