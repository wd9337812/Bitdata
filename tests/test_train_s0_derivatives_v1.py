from __future__ import annotations

import pandas as pd

from scripts.train_s0_derivatives_v1 import (
    build_metric_features,
    merge_symbol_derivatives,
)


def _metrics() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "timestamp_ms": 0,
                "sum_open_interest": 100,
                "sum_open_interest_value": 1000,
                "count_toptrader_long_short_ratio": 1.0,
                "sum_toptrader_long_short_ratio": 1.0,
                "count_long_short_ratio": 1.0,
                "sum_taker_long_short_vol_ratio": 1.0,
            },
            {
                "timestamp_ms": 300_000,
                "sum_open_interest": 110,
                "sum_open_interest_value": 1120,
                "count_toptrader_long_short_ratio": 1.1,
                "sum_toptrader_long_short_ratio": 1.2,
                "count_long_short_ratio": 0.9,
                "sum_taker_long_short_vol_ratio": 1.4,
            },
            {
                "timestamp_ms": 600_000,
                "sum_open_interest": 120,
                "sum_open_interest_value": 1250,
                "count_toptrader_long_short_ratio": 1.2,
                "sum_toptrader_long_short_ratio": 1.4,
                "count_long_short_ratio": 0.8,
                "sum_taker_long_short_vol_ratio": 1.6,
            },
        ]
    )


def test_metrics_are_available_only_after_their_five_minute_window() -> None:
    features = build_metric_features(_metrics())
    assert features.available_ms.tolist() == [300_000, 600_000, 900_000]

    candidates = pd.DataFrame(
        [
            {"open_time": 599_999, "symbol": "BTCUSDT", "direction": "LONG"},
            {"open_time": 600_000, "symbol": "BTCUSDT", "direction": "LONG"},
        ]
    )
    merged = merge_symbol_derivatives(candidates, _metrics(), pd.DataFrame())
    assert merged.iloc[0].timestamp_ms == 0
    assert merged.iloc[1].timestamp_ms == 300_000


def test_metric_features_use_only_trailing_values() -> None:
    features = build_metric_features(_metrics())
    assert pd.isna(features.iloc[0].deriv_oi_change_1)
    assert round(float(features.iloc[1].deriv_oi_change_1), 6) == 0.1
