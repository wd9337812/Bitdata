from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd


SCRIPT = Path(__file__).parents[1] / "scripts" / "benchmark_s0_daily_tail_walkforward.py"
SPEC = importlib.util.spec_from_file_location("benchmark_s0_daily_tail_walkforward", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_training_cutoff_purges_full_target_horizon() -> None:
    assert MODULE.training_cutoff(2026) == pd.Timestamp("2025-12-24", tz="UTC")


def test_single_position_uses_strongest_daily_candidate_and_no_overlap() -> None:
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2024-01-01", "2024-01-01", "2024-01-02", "2024-01-09"], utc=True
            ),
            "symbol": ["A", "B", "C", "D"],
            "confidence": [0.4, 0.8, 0.9, 0.5],
        }
    )
    result = MODULE.select_single_position(frame)
    assert result.symbol.tolist() == ["B", "D"]


def hourly(prices: list[float]) -> pd.DataFrame:
    start = pd.Timestamp("2024-01-02", tz="UTC")
    return pd.DataFrame(
        {
            "open_time": [
                int((start + pd.Timedelta(hours=index)).timestamp() * 1000)
                for index in range(len(prices))
            ],
            "open": prices,
            "high": [price * 1.001 for price in prices],
            "low": [price * 0.999 for price in prices],
            "close": prices,
        }
    )


def test_hourly_long_stop_is_filled_at_stop_price() -> None:
    frame = hourly([100.0] * 169)
    frame.loc[24, "low"] = 89.0
    result = MODULE.execute_stop_path(
        frame, pd.Timestamp("2024-01-01", tz="UTC"), 1, 0.10
    )
    assert result is not None
    assert result["exit_reason"] == "stop"
    assert abs(result["gross_return"] + 0.10) < 1e-12


def test_hourly_short_gap_uses_worse_open() -> None:
    frame = hourly([100.0] * 169)
    frame.loc[24, ["open", "high", "low", "close"]] = [115.0, 116.0, 114.0, 115.0]
    result = MODULE.execute_stop_path(
        frame, pd.Timestamp("2024-01-01", tz="UTC"), -1, 0.10
    )
    assert result is not None
    assert result["exit_reason"] == "stop_gap"
    assert abs(result["gross_return"] + 0.15) < 1e-12


def test_metrics_include_compounded_path_drawdown() -> None:
    result = MODULE.metrics(pd.Series([0.10, -0.05, 0.02]))
    assert result["trades"] == 3
    assert result["profit_factor"] == 2.4
    assert result["max_drawdown"] < 0
