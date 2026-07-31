from __future__ import annotations

import pandas as pd

from scripts.benchmark_s0_oi_flush import Candidate, annual_stress, metrics, signals


def test_flush_reversal_trades_against_price_shock() -> None:
    candidate = Candidate("x", "flush_reversal", 1, 1.0, 1.0, 1.5, 3.0, 5.0, 12)
    panel = pd.DataFrame(
        {
            "symbol": ["A", "A"],
            "available_ms": [1, 2],
            "close": [100.0, 98.0],
            "sum_open_interest": [100.0, 98.0],
            "volume_ratio_24h": [1.0, 2.0],
            "quote_volume": [100.0, 200.0],
        }
    )
    result = signals(panel, candidate)
    assert len(result) == 1
    assert result.iloc[0].direction == 1


def test_build_breakout_follows_price_shock() -> None:
    candidate = Candidate("x", "build_breakout", 1, 1.0, 1.0, 1.5, 3.0, 5.0, 12)
    panel = pd.DataFrame(
        {
            "symbol": ["A", "A"],
            "available_ms": [1, 2],
            "close": [100.0, 102.0],
            "sum_open_interest": [100.0, 102.0],
            "volume_ratio_24h": [1.0, 2.0],
            "quote_volume": [100.0, 200.0],
        }
    )
    result = signals(panel, candidate)
    assert len(result) == 1
    assert result.iloc[0].direction == 1


def test_metrics_deduct_cost() -> None:
    trades = pd.DataFrame({"gross_pct": [1.0, -0.5]})
    result = metrics(trades, 0.1)
    assert result["net_pct_points"] == 0.3
    assert result["mean_gross_bps"] == 25.0


def test_annual_stress_keeps_years_separate() -> None:
    trades = pd.DataFrame(
        {
            "entry_time": pd.to_datetime(["2022-01-01", "2023-01-01"], utc=True),
            "gross_pct": [1.0, -0.5],
        }
    )
    result = annual_stress(trades)
    assert result["2022"]["net_pct_points"] == 0.76
    assert result["2023"]["net_pct_points"] == -0.74
