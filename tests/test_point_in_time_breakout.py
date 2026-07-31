from __future__ import annotations

import pandas as pd

from scripts.benchmark_s0_point_in_time_breakout import (
    BreakoutCandidate,
    add_breakout_features,
    auction_signals,
    raw_breakout_signals,
)


def small_panel() -> pd.DataFrame:
    rows = []
    for index in range(26):
        rows.append(
            {
                "symbol": "AUSDT",
                "available_ms": 1_800_000_000_000 + index * 3_600_000,
                "open": 10.0,
                "high": 10.0,
                "low": 9.0,
                "close": 9.5 if index < 25 else 11.0,
                "quote_volume": 1_000_000.0,
                "liquidity_24h": 24_000_000.0,
                "symbol_age_days": 100.0,
                "atr_24h": 1.0,
                "ret_6h": 0.01,
                "ret_24h": 0.02,
                "ret_72h": 0.03,
            }
        )
    return pd.DataFrame(rows)


def test_channel_uses_only_prior_bars() -> None:
    result = add_breakout_features(small_panel(), (24,))
    final = result.iloc[-1]
    assert final.channel_high_24h == 10.0
    assert final.close == 11.0


def test_breakout_requires_first_crossing() -> None:
    panel = add_breakout_features(small_panel(), (24,))
    candidate = BreakoutCandidate(
        "test",
        24,
        0.5,
        20_000_000,
        1.5,
        2.0,
        24,
    )
    signals = raw_breakout_signals(panel, candidate, 30)
    assert len(signals) == 1
    assert signals.iloc[0].direction == "LONG"


def test_auction_keeps_one_signal_per_hour() -> None:
    raw = pd.DataFrame(
        {
            "available_ms": [1, 1, 2],
            "symbol": ["A", "B", "C"],
            "auction_score": [0.4, 0.9, 0.5],
            "breakout_atr": [0.2, 0.3, 0.1],
            "liquidity_24h": [10, 20, 10],
            "direction": ["LONG", "SHORT", "LONG"],
        }
    )
    result = auction_signals(raw)
    assert result.symbol.tolist() == ["B", "C"]
