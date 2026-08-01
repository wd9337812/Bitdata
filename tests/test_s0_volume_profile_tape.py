import pandas as pd
import pytest

from scripts.benchmark_s0_volume_profile_tape import (
    apply_cost,
    _exit_trade,
    _funding_return,
    metrics,
    profile_levels,
)


def test_profile_finds_heaviest_bin_and_covers_value_area():
    frame = pd.DataFrame(
        {
            "high": [100.01, 100.01, 101.01, 102.01],
            "low": [99.99, 99.99, 100.99, 101.99],
            "close": [100.0, 100.0, 101.0, 102.0],
            "quote_volume": [50.0, 50.0, 20.0, 10.0],
        }
    )
    result = profile_levels(frame)
    assert result["poc"] == pytest.approx(100.0, rel=0.002)
    assert result["coverage"] >= 0.70
    assert result["val"] <= result["poc"] <= result["vah"]


def test_exit_is_conservative_when_stop_and_target_share_bar():
    path = pd.DataFrame(
        {
            "time": pd.date_range("2025-01-01", periods=1, freq="5min", tz="UTC"),
            "open": [100.0],
            "high": [103.0],
            "low": [98.0],
            "close": [101.0],
        }
    )
    price, _, reason = _exit_trade(path, "LONG", 100.0, 102.0, 0.01)
    assert price == 99.0
    assert reason == "STOP"


def test_funding_cost_has_correct_direction():
    funding = pd.DataFrame(
        {
            "time": pd.to_datetime(["2025-01-01T08:00:00Z", "2025-01-01T16:00:00Z"]),
            "funding_rate": [0.0001, 0.0002],
        }
    )
    start = pd.Timestamp("2025-01-01T07:00:00Z")
    end = pd.Timestamp("2025-01-01T17:00:00Z")
    assert _funding_return(funding, start, end, "LONG") == pytest.approx(0.0003)
    assert _funding_return(funding, start, end, "SHORT") == pytest.approx(-0.0003)


def test_metrics_reports_cost_adjusted_expectancy():
    result = metrics(pd.DataFrame({"net_return": [0.02, -0.01, 0.01]}))
    assert result["trades"] == 3
    assert result["profit_factor"] == pytest.approx(3.0)
    assert result["net_return"] == pytest.approx(0.02)
    assert result["max_drawdown"] < 0


def test_apply_cost_does_not_change_signal_path():
    raw = pd.DataFrame({"gross_return": [0.02], "funding_return": [0.001]})
    base = apply_cost(raw, 0.0014)
    stress = apply_cost(raw, 0.0024)
    assert base.net_return.iloc[0] == pytest.approx(0.0176)
    assert stress.net_return.iloc[0] == pytest.approx(0.0166)
