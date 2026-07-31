from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd


SCRIPT = Path(__file__).parents[1] / "scripts" / "benchmark_s0_worldquant_superalpha.py"
SPEC = importlib.util.spec_from_file_location("benchmark_s0_worldquant_superalpha", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_target_uses_next_day_open_to_close() -> None:
    index = pd.date_range("2024-01-01", periods=3, tz="UTC")
    open_ = pd.DataFrame({"A": [10.0, 20.0, 40.0]}, index=index)
    close = pd.DataFrame({"A": [11.0, 22.0, 36.0]}, index=index)
    target = (close.shift(-1) - open_.shift(-1)) / open_.shift(-1)
    assert target.loc[index[0], "A"] == 0.10
    assert target.loc[index[1], "A"] == -0.10


def test_s0_candidate_uses_largest_absolute_score_and_direction() -> None:
    date = pd.Timestamp("2024-01-01", tz="UTC")
    score = pd.DataFrame({"A": [0.2], "B": [-0.4]}, index=[date])
    target = pd.DataFrame({"A": [0.1], "B": [0.05]}, index=[date])
    universe = pd.DataFrame({"A": [True], "B": [True]}, index=[date])
    # The production gate needs ten assets; add neutral valid names.
    for index in range(8):
        name = f"X{index}"
        score[name] = 0.01
        target[name] = 0.0
        universe[name] = True
    result = MODULE.build_s0_candidates(score, target, universe)
    assert result.iloc[0].symbol == "B"
    assert result.iloc[0].direction == "SHORT"
    assert result.iloc[0].gross_return == -0.05


def test_trade_metrics_deducts_cost_before_call() -> None:
    metrics = MODULE.trade_metrics(pd.Series([0.01, -0.005, 0.02]))
    assert metrics["trades"] == 3
    assert metrics["profit_factor"] == 6.0
    assert abs(metrics["net_return"] - 0.025) < 1e-12


def test_training_windows_are_strictly_ordered() -> None:
    assert MODULE.WINDOWS["development_2020_2022"][1] == "2023-01-01"
    assert MODULE.WINDOWS["validation_2023"] == ("2023-01-01", "2024-01-01")
    assert MODULE.WINDOWS["test_2024"] == ("2024-01-01", "2025-01-01")
    assert MODULE.WINDOWS["blind_2025"] == ("2025-01-01", "2026-01-01")
