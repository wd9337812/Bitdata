from __future__ import annotations

import pandas as pd

from scripts.benchmark_s0_market_tsmom_trailing import Variant, simulate


def test_simulate_preserves_entry_stop_after_trailing_moves() -> None:
    days = pd.date_range("2026-01-01", periods=15, tz="UTC")
    closes = [100.0] * 10 + [100.0, 110.0, 120.0, 130.0, 140.0]
    btc = pd.DataFrame(
        {
            "day": days,
            "open": closes,
            "high": [value + 1.0 for value in closes],
            "low": [value - 1.0 for value in closes],
            "close": closes,
            "atr_10": [2.0] * len(days),
        }
    )
    state = pd.DataFrame(
        {
            "day": days,
            "signal": [False] * 9 + [True] * 6,
        }
    )

    trades = simulate(
        btc,
        state,
        Variant(atr_days=10, atr_multiple=3.0, disaster_stop_pct=0.15, max_hold_days=4),
    )

    assert len(trades) == 1
    trade = trades.iloc[0]
    assert trade.entry_price == 100.0
    assert trade.initial_stop == 94.0
    assert trade.initial_stop_pct == 0.06
