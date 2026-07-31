import numpy as np
import pandas as pd

from scripts import benchmark_s0_daily_order_flow as subject


def hourly_day(date: str) -> pd.DataFrame:
    times = pd.date_range(date, periods=24, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "open_time": [int(value.timestamp() * 1_000) for value in times],
            "open": np.arange(24) + 100.0,
            "high": np.arange(24) + 101.0,
            "low": np.arange(24) + 99.0,
            "close": np.arange(24) + 100.5,
            "quote_volume": np.full(24, 1_000.0),
            "taker_buy_quote_volume": np.full(24, 600.0),
        }
    )


def test_daily_from_hourly_requires_complete_utc_day():
    complete = hourly_day("2026-01-01")
    incomplete = hourly_day("2026-01-02").iloc[:-1]
    daily = subject.daily_from_hourly(
        pd.concat([complete, incomplete], ignore_index=True), "BTCUSDT"
    )
    assert len(daily) == 1
    assert daily.iloc[0].symbol == "BTCUSDT"
    assert daily.iloc[0].quote_volume == 24_000.0
    assert daily.iloc[0].taker_buy_quote_volume == 14_400.0


def test_select_one_per_day_uses_expected_net_and_liquidity_tiebreak():
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-01"] * 3, utc=True),
            "prediction": [0.001, -0.01, 0.01],
            "quote_volume": [100.0, 100.0, 200.0],
            "symbol": ["NO", "LOW", "HIGH"],
        }
    )
    selected = subject.select_one_per_day(frame)
    assert selected.symbol.tolist() == ["HIGH"]
    assert selected.side.tolist() == [1]


def test_remove_top_winners_is_direction_aware():
    frame = pd.DataFrame(
        {
            "side": [1, -1, 1, -1],
            "future_return": [0.03, -0.05, -0.02, 0.01],
        }
    )
    trimmed = subject.remove_top_winners(frame, count=2)
    assert trimmed.index.tolist() == [2, 3]
