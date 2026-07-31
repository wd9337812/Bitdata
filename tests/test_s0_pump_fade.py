import numpy as np
import pandas as pd
import pytest

from scripts import benchmark_s0_pump_fade as subject


def test_event_baselines_exclude_current_pump_hour():
    hours = 1_600
    times = pd.date_range("2026-01-01", periods=hours, freq="h", tz="UTC")
    frame = pd.DataFrame(
        {
            "open_time": [int(value.timestamp() * 1_000) for value in times],
            "open": np.full(hours, 100.0),
            "high": np.r_[np.full(hours - 1, 101.0), 200.0],
            "low": np.full(hours, 99.0),
            "close": np.full(hours, 100.0),
            "quote_volume": np.r_[np.full(hours - 1, 1_000_000.0), 10_000_000.0],
            "taker_buy_quote_volume": np.full(hours, 500_000.0),
        }
    )
    events = subject.events_for_symbol(frame, "TESTUSDT")
    assert len(events) == 1
    event = events.iloc[0]
    assert event.price_increase == pytest.approx(1.0)
    assert event.volume_multiple == pytest.approx(10.0, rel=1e-3)
    assert event.available_ms == frame.open_time.iloc[-1] + 3_600_000


def test_short_path_uses_stop_first_and_linear_return():
    hourly = pd.DataFrame(
        {
            "open_time": np.arange(25) * 3_600_000,
            "open": [100.0] * 24 + [90.0],
            "high": [120.0] + [100.0] * 24,
            "low": [60.0] + [100.0] * 24,
        }
    )
    stopped = subject.execute_short_path(0, hourly)
    assert stopped["gross_return"] == -0.15
    assert stopped["exit_reason"] == "stop"

    hourly.loc[0, ["high", "low"]] = [100.0, 100.0]
    timed = subject.execute_short_path(0, hourly)
    assert timed["gross_return"] == pytest.approx(0.1)
    assert timed["exit_reason"] == "time"
