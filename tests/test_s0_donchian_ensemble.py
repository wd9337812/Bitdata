from __future__ import annotations

import pandas as pd

from scripts.benchmark_s0_donchian_ensemble import (
    donchian_positions,
    extract_single_position_trades,
)


def test_donchian_position_enters_breakout_and_exits_trailing_midpoint() -> None:
    close = pd.Series([1.0, 2.0, 3.0, 2.4, 1.9])
    upper = close.rolling(3, min_periods=3).max()
    lower = close.rolling(3, min_periods=3).min()
    midpoint = (upper + lower) / 2

    positions = donchian_positions(close, upper, midpoint)

    assert positions.tolist() == [0.0, 0.0, 1.0, 1.0, 0.0]


def test_single_position_trade_compounds_daily_returns_and_exit_cost() -> None:
    daily = pd.DataFrame(
        {
            "day": pd.to_datetime(
                ["2025-01-01", "2025-01-02", "2025-01-03"], utc=True
            ),
            "symbol": ["AAAUSDT", "AAAUSDT", None],
            "gross_return": [0.10, -0.05, 0.0],
        }
    )

    trades = extract_single_position_trades(daily)

    assert len(trades) == 1
    assert round(float(trades.iloc[0].net_return), 6) == 0.0444
