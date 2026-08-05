from __future__ import annotations

import argparse

import pandas as pd

from scripts.benchmark_s0_funding_exhaustion import signals_with_exhaustion


def _args(**kwargs) -> argparse.Namespace:
    defaults = dict(
        min_abs_funding_pct=0.10,
        min_24h_move_pct=2.0,
        max_6h_move_pct=0.30,
        min_liquidity_24h=20_000_000.0,
        min_age_days=45,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_exhaustion_filter() -> None:
    panel = pd.DataFrame(
        [
            {
                "symbol": "AUSDT", "available_ms": 100,
                "funding_rate_pct": 0.15, "funding_age_hours": 1,
                "ret_24h": 0.05, "ret_6h": 0.001,
                "liquidity_24h": 30_000_000.0, "symbol_age_days": 100,
            },
            {
                "symbol": "BUSDT", "available_ms": 200,
                "funding_rate_pct": 0.15, "funding_age_hours": 1,
                "ret_24h": 0.05, "ret_6h": 0.02,  # not stalled
                "liquidity_24h": 30_000_000.0, "symbol_age_days": 100,
            },
            {
                "symbol": "CUSDT", "available_ms": 300,
                "funding_rate_pct": 0.05, "funding_age_hours": 1,
                "ret_24h": 0.05, "ret_6h": 0.001,
                "liquidity_24h": 30_000_000.0, "symbol_age_days": 100,
            },
        ]
    )
    signals = signals_with_exhaustion(panel, _args())
    assert len(signals) == 1
    assert signals.iloc[0]["symbol"] == "AUSDT"
    assert signals.iloc[0]["direction"] == "SHORT"
