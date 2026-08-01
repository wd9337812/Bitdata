from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from benchmark_s0_global_direction_gate import _direction_stats, _levels  # noqa: E402


def test_direction_stats_only_reads_prior_window() -> None:
    day = pd.Timestamp("2026-08-01", tz="UTC")
    history = {
        "LONG": [(pd.Timestamp("2026-07-15", tz="UTC"), 0.1), (pd.Timestamp("2026-06-01", tz="UTC"), 0.2)],
        "SHORT": [],
    }
    assert _direction_stats(history, "LONG", day, 30) == pytest.approx([0.1])


def test_levels_are_directional_and_three_r() -> None:
    series = pd.DataFrame(
        [{"open": 100.0, "atr_14": 1.0}, {"open": 100.0, "atr_14": 1.0}]
    )
    long_stop, long_target = _levels(series, 0, "LONG")
    short_stop, short_target = _levels(series, 0, "SHORT")
    assert long_stop == pytest.approx(98.0)
    assert long_target == pytest.approx(106.0)
    assert short_stop == pytest.approx(102.0)
    assert short_target == pytest.approx(94.0)
