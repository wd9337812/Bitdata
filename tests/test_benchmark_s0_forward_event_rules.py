from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from scripts.benchmark_s0_forward_event_rules import (
    btc_events,
    settle_on_hourly,
    volume_events,
)


def _args(**kwargs) -> argparse.Namespace:
    defaults = dict(
        funding_threshold_pct=0.05,
        funding_z=2.0,
        vol_z=3.0,
        btc_z=3.0,
        cost_pct=0.24,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _hourly_frame(n: int = 800, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.005, n))
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * (1.0 + rng.uniform(0.0, 0.002, n))
    low = np.minimum(open_, close) * (1.0 - rng.uniform(0.0, 0.002, n))
    start_ms = int(pd.Timestamp("2022-01-01").value // 1_000_000)
    return pd.DataFrame(
        {
            "open_time": start_ms + np.arange(n) * 3_600_000,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "quote_volume": rng.uniform(1e6, 1e7, n),
        }
    )


def test_volume_events_requires_vol_z_threshold_and_direction() -> None:
    bars = _hourly_frame()
    bars.loc[100, "quote_volume"] = bars.quote_volume.iloc[:199].mean() * 20.0
    bars.loc[100, "close"] = bars.loc[100, "open"] * 1.01
    events = volume_events(bars, _args())
    assert len(events) == 1
    assert events.iloc[0]["direction"] == 1


def test_btc_events_requires_abs_z_threshold() -> None:
    n = 700
    phase = np.arange(n) * 2.0 * np.pi / 8.0
    close = 100.0 + 1.0 * np.sin(phase)
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * 1.001
    low = np.minimum(open_, close) * 0.999
    volume = np.full(n, 1e6)
    start_ms = int(pd.Timestamp("2022-01-01").value // 1_000_000)
    bars = pd.DataFrame(
        {
            "open_time": start_ms + np.arange(n) * 3_600_000,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "quote_volume": volume,
        }
    )
    # Alternating 4h rets have high rolling std, so no impulse triggers.
    assert len(btc_events(bars, _args())) == 0

    bars.loc[500:503, "close"] = bars.loc[500:503, "close"].to_numpy() * np.array(
        [1.05, 1.10, 1.15, 1.20]
    )
    events = btc_events(bars, _args())
    assert len(events) >= 1
    assert (events["direction"] == 1).any()


def test_settle_on_hourly_uses_1h_entry_delay_and_direction() -> None:
    bars = _hourly_frame(50)
    events = pd.DataFrame(
        [
            {"symbol": "TESTUSDT", "ts": int(bars.open_time.iloc[2]), "direction": 1},
            {"symbol": "TESTUSDT", "ts": int(bars.open_time.iloc[3]), "direction": -1},
        ]
    )
    result = settle_on_hourly(events, bars, hold_hours=5)
    assert len(result) == 2
    assert result.iloc[0]["gross_pct"] == (
        (bars.close.iloc[7] / bars.open.iloc[3] - 1.0) * 100.0
    )
    assert result.iloc[1]["gross_pct"] == (
        -(bars.close.iloc[8] / bars.open.iloc[4] - 1.0) * 100.0
    )
    assert result["mfe_pct"].min() >= 0.0
