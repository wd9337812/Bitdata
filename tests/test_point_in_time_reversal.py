from __future__ import annotations

import pandas as pd

from scripts.benchmark_s0_point_in_time_reversal import (
    ReversalCandidate,
    add_reversal_features,
    reversal_signals,
)


def test_reversal_volatility_is_shifted() -> None:
    rows = []
    for index in range(55):
        close = 100.0 + (index % 2)
        rows.append(
            {
                "symbol": "AUSDT",
                "available_ms": 1_800_000_000_000 + index * 3_600_000,
                "open": close,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "quote_volume": 1_000_000.0,
                "liquidity_24h": 24_000_000.0,
                "symbol_age_days": 100.0,
                "atr_24h": 1.0,
                "ret_6h": 0.0,
                "ret_24h": 0.0,
                "ret_72h": 0.0,
            }
        )
    panel = pd.DataFrame(rows)
    baseline = add_reversal_features(panel)
    before = baseline.iloc[-1].prior_volatility_48h
    panel.loc[panel.index[-1], "close"] = 150.0
    changed = add_reversal_features(panel)
    assert changed.iloc[-1].prior_volatility_48h == before


def test_upward_shock_creates_short_signal() -> None:
    rows = []
    for index in range(55):
        close = 100.0 + (index % 2)
        if index == 54:
            close = 120.0
        rows.append(
            {
                "symbol": "AUSDT",
                "available_ms": 1_800_000_000_000 + index * 3_600_000,
                "open": close,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "quote_volume": 2_000_000.0,
                "liquidity_24h": 48_000_000.0,
                "symbol_age_days": 100.0,
                "atr_24h": 1.0,
                "ret_6h": 0.0,
                "ret_24h": 0.0,
                "ret_72h": 0.0,
            }
        )
    panel = add_reversal_features(pd.DataFrame(rows))
    candidate = ReversalCandidate(
        "test",
        formation_hours=1,
        shock_z=2.5,
        minimum_volume_ratio=0.5,
        minimum_liquidity_24h=20_000_000,
        stop_atr=1.5,
        target_fraction=0.5,
        hold_hours=4,
    )
    signals = reversal_signals(panel, candidate, 30)
    assert len(signals) == 1
    assert signals.iloc[0].direction == "SHORT"
