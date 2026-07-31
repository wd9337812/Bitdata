from __future__ import annotations

import pandas as pd

from scripts.benchmark_s0_donchian_long_short import (
    extract_trades,
    long_short_positions,
)


def test_long_short_positions_can_enter_and_reverse() -> None:
    close = pd.Series([2.0, 3.0, 4.0, 3.0, 2.0, 1.0])
    upper = close.rolling(3, min_periods=3).max()
    lower = close.rolling(3, min_periods=3).min()
    midpoint = (upper + lower) / 2

    positions = long_short_positions(close, upper, lower, midpoint)

    assert positions.tolist() == [0.0, 0.0, 1.0, -1.0, -1.0, -1.0]


def test_extract_trades_splits_direction_change() -> None:
    daily = pd.DataFrame(
        {
            "day": pd.to_datetime(
                ["2025-01-01", "2025-01-02", "2025-01-03"], utc=True
            ),
            "symbol": ["AAAUSDT", "AAAUSDT", "AAAUSDT"],
            "direction": [1, 1, -1],
            "gross_return": [0.02, 0.01, 0.03],
        }
    )

    trades = extract_trades(daily)

    assert len(trades) == 2
    assert trades.direction.tolist() == [1, -1]
