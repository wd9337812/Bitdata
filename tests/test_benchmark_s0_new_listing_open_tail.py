from __future__ import annotations

import argparse

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from scripts.benchmark_s0_new_listing_open_tail import (
    Structure,
    bootstrap_symbols,
    metrics,
    simulate_account,
    simulate_one,
    simulate_structure,
)


def _minute_frame(prices: list[float], start_ms: int = 0) -> pd.DataFrame:
    rows = []
    for index, price in enumerate(prices):
        rows.append(
            {
                "open_time": start_ms + index * 60_000,
                "open": float(price),
                "high": float(price),
                "low": float(price),
                "close": float(price),
            }
        )
    return pd.DataFrame(rows)


def _args(**kwargs) -> argparse.Namespace:
    defaults = dict(
        initial_equity=10.0,
        hard_stop_equity=5.0,
        position_fraction=1.0,
        bootstrap_samples=100,
        bootstrap_seed=7,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_simulate_one_stop_adverse_first() -> None:
    frame = _minute_frame([100.0, 100.0, 100.0])
    frame.loc[1, "low"] = 80.0
    frame.loc[1, "high"] = 140.0
    structure = Structure("t", 0, 15.0, 50.0, None, None)
    result = simulate_one(frame, structure, 0.0)
    assert result is not None
    assert result["outcome"] == "STOP"
    assert result["exit"] == 85.0


def test_simulate_one_target_and_trail() -> None:
    frame = _minute_frame([100.0, 120.0, 110.0, 100.0])
    target = Structure("t", 0, 15.0, 20.0, None, None)
    result = simulate_one(frame, target, 0.0)
    assert result is not None and result["outcome"] == "TARGET"
    trail = Structure("t", 0, 20.0, None, 20.0, 15.0)
    result = simulate_one(frame, trail, 0.0)
    assert result is not None and result["outcome"] == "TRAIL"


def test_simulate_structure_and_metrics(tmp_path) -> None:
    winner = _minute_frame([100.0] * 10)
    winner.loc[1, "high"] = 150.0
    loser = _minute_frame([100.0] * 10)
    loser.loc[1, "low"] = 80.0
    listings = [
        {
            "symbol": "TESTUSDT",
            "listing_ms": int(pd.Timestamp("2025-01-01", tz="UTC").timestamp() * 1000),
            "frame": winner,
        },
        {
            "symbol": "TEST2USDT",
            "listing_ms": int(pd.Timestamp("2025-02-01", tz="UTC").timestamp() * 1000),
            "frame": loser,
        }
    ]
    structure = Structure("open_s15_tp50", 0, 15.0, 50.0, None, None)
    trades = simulate_structure(listings, structure, 0.60, 0.0)
    assert len(trades) == 2
    assert set(trades.outcome) == {"TARGET", "STOP"}
    report = metrics(trades)
    assert report["profit_factor"] == pytest.approx(49.4 / 15.6, abs=1e-3)


def test_bootstrap_symbols_positive() -> None:
    frame = pd.DataFrame(
        {
            "symbol": ["A", "A", "B", "B"],
            "year": [2025, 2025, 2025, 2025],
            "net_pct": [10.0, 10.0, 5.0, 5.0],
            "outcome": ["TARGET", "TARGET", "TARGET", "TARGET"],
            "hold_minutes": [60, 60, 60, 60],
        }
    )
    result = bootstrap_symbols(frame, 100, 7)
    assert result["positive_probability"] == 1.0


def test_simulate_account_compounds_and_ruins() -> None:
    frame = pd.DataFrame(
        [
            {
                "symbol": "A",
                "listing_ms": 0,
                "net_pct": 50.0,
                "hold_minutes": 60,
                "exit_ms": 3_600_000,
            },
            {
                "symbol": "B",
                "listing_ms": 60_000,
                "net_pct": -60.0,
                "hold_minutes": 60,
                "exit_ms": 3_660_000,
            },
            {
                "symbol": "C",
                "listing_ms": 120_000,
                "net_pct": 50.0,
                "hold_minutes": 60,
                "exit_ms": 3_720_000,
            },
        ]
    )
    result = simulate_account(frame, _args())
    # A wins (+50%), B loses while busy? No: B entry 60s < A exit 60min -> skipped,
    # C entry 120s also skipped -> only one trade.
    assert result["trades"] == 1
    assert result["final_equity"] == pytest.approx(15.0)
