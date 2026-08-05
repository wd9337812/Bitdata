from __future__ import annotations

import argparse

import pandas as pd

from scripts.benchmark_s0_30d_confluence import (
    apply_named_filter,
    confirmations,
)


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        min_ret6h_pct=0.5,
        min_breadth_pct=0.5,
        min_ret24h_pct=5.0,
    )


def test_confirmations_and_filters() -> None:
    signals = pd.DataFrame(
        {
            "symbol": ["A", "B", "C"],
            "direction": ["LONG", "LONG", "SHORT"],
            "ret_6h": [0.01, 0.001, -0.01],
            "ret_24h": [0.06, 0.001, -0.06],
            "market_breadth": [0.01, 0.001, -0.01],
        }
    )
    enriched = confirmations(signals, _args())
    # A: acceleration+breadth+strong = 3; B: none; C: acceleration+strong+breadth = 3.
    assert enriched.loc[0, "confirmations"] == 3
    assert enriched.loc[1, "confirmations"] == 0
    need_2 = apply_named_filter(enriched, "need_2", 2)
    assert set(need_2.symbol) == {"A", "C"}
    pair = apply_named_filter(enriched, "accel_and_breadth", "acceleration&breadth")
    assert set(pair.symbol) == {"A", "C"}
