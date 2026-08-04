from __future__ import annotations

import gzip

import numpy as np
import pandas as pd
import pytest

from scripts.download_bybit_mt4_1m import month_url, parse_month
from scripts.research_s0_cross_exchange_dislocation import (
    build_signal_matrices,
    select_events_fast,
    simulate_premium_revert,
)


def test_month_url_matches_official_pattern() -> None:
    assert month_url("BTCUSDT", 2025, 1) == (
        "https://public.bybit.com/kline_for_metatrader4/BTCUSDT/2025/"
        "BTCUSDT_1_2025-01-01_2025-01-31.csv.gz"
    )


def test_parse_month_shifts_utc_plus_3(tmp_path) -> None:
    content = (
        "2025.01.01 00:00,100.0,101.0,99.0,100.5,10.0\n"
        "2025.01.01 00:01,100.5,102.0,100.0,101.0,20.0\n"
    )
    path = tmp_path / "BTCUSDT_1_2025-01.csv.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(content)
    frame = parse_month(path, "BTCUSDT")
    assert len(frame) == 2
    assert frame.open_time.iloc[0] == int(
        pd.Timestamp("2024-12-31 21:00", tz="UTC").timestamp() * 1000
    )
    assert frame.close.iloc[1] == pytest.approx(101.0)


def test_simulate_premium_revert_is_adverse_first() -> None:
    rows = []
    for index in range(10):
        rows.append(
            {
                "open_time": index * 60_000,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "premium_pct": 0.2,
            }
        )
    frame = pd.DataFrame(rows)
    # Minute 2: premium reverts (0.1) AND widens (0.41) in the same bar.
    frame.loc[2, "premium_pct"] = 0.41
    result = simulate_premium_revert(
        frame,
        entry_ms=0,
        entry_premium=0.2,
        direction=-1,
        revert_ratio=0.5,
        stop_ratio=2.0,
        max_hold=10,
    )
    assert result is not None
    assert result[2] == "STOP"


def test_select_events_fast_picks_largest_premium() -> None:
    grid = [5 * 60_000]
    panel = {
        "AUSDT": {
            "open_time": np.array([0, 4 * 60_000], dtype="int64"),
            "premium": np.array([0.1, 0.2], dtype="float64"),
            "z": np.array([3.0, 3.0], dtype="float64"),
            "vol21": np.array([1e8, 1e8], dtype="float64"),
        },
        "BUSDT": {
            "open_time": np.array([0, 4 * 60_000], dtype="int64"),
            "premium": np.array([0.1, 0.5], dtype="float64"),
            "z": np.array([3.0, 3.0], dtype="float64"),
            "vol21": np.array([1e8, 1e8], dtype="float64"),
        },
    }
    premium_matrix, z_matrix, vol_matrix, symbols = build_signal_matrices(
        panel, grid
    )
    events = select_events_fast(
        premium_matrix,
        z_matrix,
        vol_matrix,
        symbols,
        grid,
        threshold_pct=0.1,
        z_threshold=2.0,
        min_volume=1e6,
    )
    assert len(events) == 1
    assert events[0][1] == "BUSDT"
    assert events[0][2] == -1
