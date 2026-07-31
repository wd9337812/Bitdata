from __future__ import annotations

import pandas as pd

from scripts.benchmark_s0_major_trend import strategy_daily


def test_strategy_daily_applies_next_day_return_and_turnover_cost() -> None:
    frame = pd.DataFrame(
        {
            "day": pd.to_datetime(
                ["2025-01-01", "2025-01-02", "2025-01-03"], utc=True
            ),
            "symbol": ["BTCUSDT"] * 3,
            "close": [1.0, 2.0, 3.0],
            "next_return": [1.0, 0.5, 0.0],
        }
    )

    result = strategy_daily(frame, (2,), long_short=False)

    assert result.direction.tolist() == [0, 1, 1]
    assert round(float(result.iloc[1].net_return), 6) == 0.4994
