from __future__ import annotations

import gzip

import numpy as np
import pandas as pd

from scripts.download_bybit_trades_10s import aggregate_day
from scripts.research_s0_subminute_pair import select_events


def test_aggregate_day_builds_10s_bars(tmp_path) -> None:
    content = (
        "timestamp,symbol,side,size,price\n"
        "1780272000.1,SOLUSDT,Buy,10,100.0\n"
        "1780272000.2,SOLUSDT,Buy,20,101.0\n"
        "1780272000.8,SOLUSDT,Sell,5,99.5\n"
        "1780272010.5,SOLUSDT,Buy,7,102.0\n"
    )
    path = tmp_path / "SOLUSDT2026-06-01.csv.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(content)
    frame = aggregate_day(path, "SOLUSDT", "2026-06-01")
    assert len(frame) == 2
    assert frame.bin_ms.iloc[0] == 1780272000000
    assert frame.open.iloc[0] == 100.0
    assert frame.high.iloc[0] == 101.0
    assert frame.low.iloc[0] == 99.5
    assert frame.close.iloc[0] == 99.5
    assert frame.volume.iloc[0] == 35.0


def test_select_events_detects_large_premium() -> None:
    rng = np.random.default_rng(3)
    n = 500
    frame = pd.DataFrame(
        {
            "bin_ms": np.arange(n, dtype="int64") * 10_000,
            "close": 100.0,
            "bybit_close": 100.0,
            "premium_pct": rng.normal(0.0, 0.01, n),
        }
    )
    frame.loc[300, "premium_pct"] = 0.4
    events = select_events(
        frame, threshold_pct=0.2, z_threshold=3.0, z_window_bars=120
    )
    assert len(events) >= 1
    assert events[0][1] == ""
    assert events[0][2] == -1
