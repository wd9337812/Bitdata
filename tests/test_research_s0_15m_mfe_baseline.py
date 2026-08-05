from __future__ import annotations

import argparse

import pandas as pd

from scripts.research_s0_15m_mfe_baseline import (
    fifteen_minute,
    signals_for,
    simulate,
)


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        vol_z=2.0,
        taker_imbalance=0.15,
        min_move_pct=1.0,
        stop_pct=1.0,
        tp_r=3.0,
        hold_bars=8,
        cost_pct=0.24,
    )


def test_15m_signal_and_simulate() -> None:
    rows = []
    for minute in range(2000):
        rows.append(
            {
                "open_time": minute * 60_000,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0 + minute * 0.0001,
                "quote_volume": 30_000_000.0 + (minute % 7) * 1_000_000.0,
                "taker_buy_quote_volume": 15_000_000.0 + (minute % 7) * 500_000.0,
            }
        )
    frame = pd.DataFrame(rows)
    # Signal at the 15m bar starting minute 1500.
    frame.loc[1500, "open"] = 100.0
    frame.loc[1500, "high"] = 104.0
    frame.loc[1500, "low"] = 99.5
    frame.loc[1500, "close"] = 102.0
    frame.loc[1514, "close"] = 102.0
    frame.loc[1500, "quote_volume"] = 900_000_000.0
    frame.loc[1500, "taker_buy_quote_volume"] = 900_000_000.0
    bars = fifteen_minute(frame)
    args = _args()
    signals = signals_for(bars, args)
    assert not signals.empty
    signals["symbol"] = "TESTUSDT"
    trades = simulate(signals, bars, args)
    assert len(trades) >= 1
    assert trades.iloc[0]["direction"] == 1
