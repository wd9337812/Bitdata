from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from benchmark_s0_auction_dual_state import (  # noqa: E402
    exit_path,
    structural_levels,
    value_area_overlap,
)


def test_value_area_overlap_uses_narrower_profile() -> None:
    first = {"val": 90.0, "vah": 110.0}
    second = {"val": 95.0, "vah": 105.0}
    assert value_area_overlap(first, second) == pytest.approx(1.0)


def test_value_area_overlap_is_zero_when_disjoint() -> None:
    assert value_area_overlap(
        {"val": 90.0, "vah": 95.0}, {"val": 100.0, "vah": 105.0}
    ) == 0.0


def test_acceptance_levels_use_two_r_target() -> None:
    row = type("Row", (), {"lane": "VALUE_ACCEPTANCE", "direction": "LONG", "prev_vah": 100.0})
    stop, target = structural_levels(row, 100.5)
    assert stop == pytest.approx(99.9)
    assert target == pytest.approx(101.7)


def test_acceptance_rejects_overextended_entry() -> None:
    row = type("Row", (), {"lane": "VALUE_ACCEPTANCE", "direction": "LONG", "prev_vah": 100.0})
    assert structural_levels(row, 102.0) is None


def test_exit_path_resolves_same_bar_ambiguity_against_strategy() -> None:
    path = pd.DataFrame(
        [{"time": pd.Timestamp("2026-01-01T00:05:00Z"), "open": 100.0, "high": 103.0, "low": 98.0, "close": 101.0}]
    )
    price, _, reason = exit_path(path, "LONG", stop=99.0, target=102.0)
    assert price == 99.0
    assert reason == "STOP"
