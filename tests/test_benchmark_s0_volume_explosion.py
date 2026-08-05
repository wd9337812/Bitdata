from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from scripts.benchmark_s0_volume_explosion import (
    hourly_bars,
    metrics,
    signals_for,
    simulate,
)


def _args(**kwargs) -> argparse.Namespace:
    defaults = dict(
        volume_ratio=5.0,
        taker_imbalance=0.15,
        min_24h_volume_usdt=20_000_000.0,
        stop_atr=2.5,
        target_r=10.0,
        max_hold_hours=120,
        embargo_hours=72,
        base_cost_pct=0.12,
        stress_cost_pct=0.24,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _hourly() -> pd.DataFrame:
    hours = 900
    rows = []
    for index in range(hours):
        rows.append(
            {
                "open_time": index * 3_600_000,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0 + index * 0.001,
                "quote_volume": 30_000_000.0,
                "taker_buy_quote_volume": 15_000_000.0,
            }
        )
    hourly = pd.DataFrame(rows)
    # Signal hour at index 800: 20x volume, all-buy taker, strong green candle.
    hourly.loc[800, "open"] = 100.0
    hourly.loc[800, "high"] = 105.0
    hourly.loc[800, "low"] = 99.5
    hourly.loc[800, "close"] = 104.0
    hourly.loc[800, "quote_volume"] = 200_000_000.0
    hourly.loc[800, "taker_buy_quote_volume"] = 200_000_000.0
    return hourly_bars(hourly)


def test_signals_and_simulate() -> None:
    hourly = _hourly()
    args = _args()
    signals = signals_for(hourly, args)
    assert not signals.empty
    signals["symbol"] = "TESTUSDT"
    trades = simulate(signals, hourly, args)
    assert len(trades) >= 1
    assert trades.iloc[0]["direction"] == 1


def test_metrics_negative_window() -> None:
    trades = pd.DataFrame(
        {
            "symbol": ["A", "B"],
            "entry_ms": [0, 1],
            "exit_ms": [2, 3],
            "direction": [1, 1],
            "gross_pct": [-1.0, -1.0],
            "outcome": ["STOP", "STOP"],
            "vol_ratio": [5.0, 5.0],
            "taker_imbalance": [0.5, 0.5],
        }
    )
    result = metrics(trades, 0.24)
    assert result["trades"] == 2
    assert result["profit_factor"] == 0.0
