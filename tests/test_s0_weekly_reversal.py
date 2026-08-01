import pandas as pd
import pytest

from scripts.benchmark_s0_weekly_reversal import Parameters, indicators, metrics, _path_exit


def test_indicators_shift_formation_and_liquidity():
    frame = pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=80, freq="D", tz="UTC"),
        "open": range(1, 81), "high": range(2, 82), "low": range(1, 81),
        "close": range(1, 81), "quote_volume": [1_000_000] * 80,
    })
    result = indicators(frame, Parameters(56, 7, 0.1, 0.2))
    assert pd.isna(result.iloc[56].formation_return)
    assert result.iloc[-1].liquidity_30d == 30_000_000


def test_stop_uses_conservative_stop_first_path():
    frame = pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=2, freq="D", tz="UTC"),
        "open": [100.0, 100.0], "high": [101.0, 101.0],
        "low": [99.0, 80.0], "close": [100.0, 95.0],
    })
    price, _, reason = _path_exit(frame, "LONG", 0.1)
    assert price == 90.0
    assert reason == "STOP"


def test_short_stop_is_above_entry():
    frame = pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=1, freq="D", tz="UTC"),
        "open": [100.0], "high": [111.0], "low": [99.0], "close": [105.0],
    })
    price, _, reason = _path_exit(frame, "SHORT", 0.1)
    assert price == pytest.approx(110.0)
    assert reason == "STOP"


def test_metrics_reports_pf_and_drawdown():
    result = metrics(pd.DataFrame({"net_return": [0.1, -0.05, 0.02]}))
    assert result["trades"] == 3
    assert result["profit_factor"] > 1
    assert result["max_drawdown"] < 0
