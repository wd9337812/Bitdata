import numpy as np
import pandas as pd

from scripts import benchmark_s0_intraday_event_walkforward as subject


def test_path_return_treats_same_bar_stop_and_take_as_stop():
    entry = np.array([100.0])
    result = subject.path_return(
        entry,
        [np.array([109.0])],
        [np.array([94.0])],
        np.array([105.0]),
        1,
    )
    assert result[0] == -subject.STOP_PCT


def test_short_path_can_take_profit():
    result = subject.path_return(
        np.array([100.0]),
        [np.array([101.0])],
        [np.array([91.0])],
        np.array([95.0]),
        -1,
    )
    assert result[0] == subject.TAKE_PCT


def test_select_trades_keeps_only_one_overlapping_position():
    start = pd.Timestamp("2025-01-01", tz="UTC").value // 1_000_000
    frame = pd.DataFrame(
        {
            "available_ms": [start, start + 4 * 3_600_000, start + 24 * 3_600_000],
            "confidence": [0.9, 0.8, 0.85],
            "margin": [0.4, 0.3, 0.35],
            "p_none": [0.1, 0.1, 0.1],
            "p_long": [0.9, 0.8, 0.85],
            "p_short": [0.1, 0.2, 0.15],
            "liquidity_24h": [100.0, 100.0, 100.0],
            "side": [1, 1, 1],
            "long_gross": [0.08, 0.08, 0.08],
            "short_gross": [-0.05, -0.05, -0.05],
        }
    )
    trades = subject.select_trades(frame, 0.0)
    assert list(trades.available_ms) == [start, start + 24 * 3_600_000]


def test_metrics_include_profit_factor_and_drawdown():
    result = subject.metrics(pd.Series([0.1, -0.05, 0.02]))
    assert result["profit_factor"] == 2.4
    assert result["max_drawdown"] < 0
