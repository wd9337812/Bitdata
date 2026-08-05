from __future__ import annotations

import gzip

import pandas as pd

from scripts.audit_s0_30d_dislocation_filter import apply_filter
from scripts.build_s0_30d_bybit_premium import aggregate_1m


def test_aggregate_1m_builds_minute_closes(tmp_path) -> None:
    content = (
        "timestamp,symbol,side,size,price\n"
        "1780272000.1,SOLUSDT,Buy,10,100.0\n"
        "1780272000.8,SOLUSDT,Buy,20,101.0\n"
        "1780272060.5,SOLUSDT,Buy,7,102.0\n"
    )
    path = tmp_path / "SOLUSDT2026-06-01.csv.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(content)
    frame = aggregate_1m(path)
    assert len(frame) == 2
    assert frame.bin_ms.iloc[0] == 1780272000000
    assert frame.bybit_close.iloc[0] == 101.0
    assert frame.bybit_close.iloc[1] == 102.0


def test_apply_filter_abs_and_directional() -> None:
    signals = pd.DataFrame(
        {
            "symbol": ["SOLUSDT", "SOLUSDT", "DOGEUSDT"],
            "available_ms": [1, 2, 3],
            "direction": ["LONG", "SHORT", "LONG"],
            "other": [0, 0, 0],
        }
    )
    premium = pd.DataFrame(
        {
            "symbol": ["SOLUSDT", "SOLUSDT", "DOGEUSDT"],
            "available_ms": [1, 2, 3],
            "delay_minutes": [5, 5, 5],
            "premium_pct": [0.05, -0.05, 0.30],
        }
    )
    baseline = apply_filter(signals, premium, "baseline", None, None, 5)
    assert len(baseline) == 3
    abs_filtered = apply_filter(signals, premium, "abs<=0.10", 0.10, None, 5)
    assert len(abs_filtered) == 2
    dir_filtered = apply_filter(signals, premium, "dir<=0.05", None, 0.05, 5)
    # LONG premium 0.05 passes (<=0.05); SHORT premium -0.05 passes (>= -0.05);
    # LONG premium 0.30 fails.
    assert len(dir_filtered) == 2
