from types import SimpleNamespace

import pandas as pd
import pytest

from scripts import benchmark_s0_daily_order_flow_short_oof as subject


def test_symbol_fold_is_deterministic_and_bounded():
    first = subject.symbol_fold("BTCUSDT")
    assert first == subject.symbol_fold("BTCUSDT")
    assert 0 <= first < subject.FOLDS


def test_select_short_per_day_rejects_longs_and_uses_strongest_short():
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-01"] * 3, utc=True),
            "prediction": [-0.001, -0.01, 0.02],
            "quote_volume": [100.0, 100.0, 1_000.0],
            "symbol": ["TOO_SMALL", "SHORT", "LONG"],
        }
    )
    selected = subject.select_short_per_day(frame)
    assert selected.symbol.tolist() == ["SHORT"]
    assert selected.side.tolist() == [-1]


def test_execute_path_is_stop_first_when_both_touch():
    entry = pd.Timestamp("2026-01-02", tz="UTC")
    exit_time = entry + pd.Timedelta(days=1)
    times = pd.date_range(entry, exit_time, freq="h")
    hourly = pd.DataFrame(
        {
            "open_time": [int(value.timestamp() * 1_000) for value in times],
            "open": [100.0] * len(times),
            "high": [120.0] + [100.0] * (len(times) - 1),
            "low": [60.0] + [100.0] * (len(times) - 1),
        }
    )
    trade = SimpleNamespace(entry_time=entry, exit_time=exit_time)
    result = subject.execute_path(trade, hourly)
    assert result == {"gross_return": -0.15, "exit_reason": "stop"}


def test_execute_path_uses_linear_usdt_futures_short_return():
    entry = pd.Timestamp("2026-01-02", tz="UTC")
    exit_time = entry + pd.Timedelta(days=1)
    times = pd.date_range(entry, exit_time, freq="h")
    prices = [100.0] * (len(times) - 1) + [90.0]
    hourly = pd.DataFrame(
        {
            "open_time": [int(value.timestamp() * 1_000) for value in times],
            "open": prices,
            "high": prices,
            "low": prices,
        }
    )
    trade = SimpleNamespace(entry_time=entry, exit_time=exit_time)
    result = subject.execute_path(trade, hourly)
    assert result["exit_reason"] == "time"
    assert result["gross_return"] == pytest.approx(0.1)
