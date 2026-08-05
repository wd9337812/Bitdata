from __future__ import annotations

import argparse

import pandas as pd
import pytest

from scripts.benchmark_s0_new_listing_first_hour import simulate_one


def _args(**kwargs) -> argparse.Namespace:
    defaults = dict(
        min_first_hour_pct=5.0,
        stop_pct=15.0,
        target_pct=30.0,
        hold_hours=72,
        base_cost_pct=0.12,
        stress_cost_pct=0.24,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_simulate_one_first_hour_rule() -> None:
    rows = []
    for minute in range(200):
        rows.append(
            {
                "open_time": minute * 60_000,
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
            }
        )
    frame = pd.DataFrame(rows)
    # First hour closes +10% (minute 59), entry at minute 60.
    frame.loc[59, "close"] = 1.10
    frame.loc[60, "open"] = 1.10
    frame.loc[61, "low"] = 0.90  # triggers 15% stop at 0.935
    result = simulate_one(frame, _args())
    assert result is not None
    assert result["direction"] == 1
    assert result["first_hour_pct"] == pytest.approx(10.0)
    assert result["outcome"] == "STOP"
    assert result["gross_pct"] == pytest.approx(-15.0)


def test_simulate_one_skips_small_first_hour() -> None:
    rows = []
    for minute in range(100):
        rows.append(
            {
                "open_time": minute * 60_000,
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
            }
        )
    frame = pd.DataFrame(rows)
    frame.loc[59, "close"] = 1.02
    assert simulate_one(frame, _args()) is None
