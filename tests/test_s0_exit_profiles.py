from __future__ import annotations

import pandas as pd

from scripts.benchmark_s0_exit_profiles import relabel_profile


def test_relabel_profile_uses_stop_first_for_same_minute_ambiguity():
    minute = pd.DataFrame(
        {
            "open_time": [300_000, 360_000],
            "open": [100.0, 100.0],
            "high": [102.0, 100.0],
            "low": [98.0, 100.0],
            "close": [101.0, 100.0],
        }
    )
    candidates = pd.DataFrame(
        {
            "open_time": [0],
            "entry": [100.0],
            "stop": [99.15],
            "direction": ["LONG"],
        }
    )
    result = relabel_profile(
        minute,
        candidates,
        stop_atr=0.85,
        take_profit_r=1.05,
        hold_minutes=1,
    )

    assert result.iloc[0].outcome == "STOP"
    assert result.iloc[0].net_pct < -0.9


def test_relabel_profile_changes_time_exit_with_holding_window():
    minute = pd.DataFrame(
        {
            "open_time": [300_000, 360_000, 420_000],
            "open": [100.0, 100.2, 100.5],
            "high": [100.3, 100.6, 100.8],
            "low": [99.9, 100.1, 100.4],
            "close": [100.2, 100.5, 100.7],
        }
    )
    candidates = pd.DataFrame(
        {
            "open_time": [0],
            "entry": [100.0],
            "stop": [99.15],
            "direction": ["LONG"],
        }
    )
    one = relabel_profile(
        minute,
        candidates,
        stop_atr=2.0,
        take_profit_r=3.0,
        hold_minutes=1,
    )
    three = relabel_profile(
        minute,
        candidates,
        stop_atr=2.0,
        take_profit_r=3.0,
        hold_minutes=3,
    )

    assert three.iloc[0].net_pct > one.iloc[0].net_pct
