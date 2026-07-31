from __future__ import annotations

import pandas as pd

from scripts.download_binance_um_metrics import aggregate_metrics, tasks


def test_tasks_are_inclusive() -> None:
    result = tasks(["btcusdt"], "2026-01-01", "2026-01-02")
    assert [(item.symbol, item.date) for item in result] == [
        ("BTCUSDT", "2026-01-01"),
        ("BTCUSDT", "2026-01-02"),
    ]


def test_metrics_are_available_on_hour_boundary() -> None:
    frame = pd.DataFrame(
        {
            "create_time": ["2026-01-01 00:05:00", "2026-01-01 00:55:00"],
            "symbol": ["BTCUSDT", "BTCUSDT"],
            "sum_open_interest": [100.0, 110.0],
            "sum_open_interest_value": [1000.0, 1200.0],
            "count_toptrader_long_short_ratio": [1.1, 1.2],
            "sum_toptrader_long_short_ratio": [1.0, 0.9],
            "count_long_short_ratio": [1.0, 1.1],
            "sum_taker_long_short_vol_ratio": [0.8, 1.3],
        }
    )
    result = aggregate_metrics(frame)
    assert len(result) == 1
    assert result.loc[0, "available_ms"] == int(
        pd.Timestamp("2026-01-01 01:00:00Z").timestamp() * 1000
    )
    assert result.loc[0, "sum_open_interest"] == 110.0
    assert result.loc[0, "sum_taker_long_short_vol_ratio"] == 1.3
