from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts.research_s0_intraday_xmom import (
    build_panel,
    build_trades,
    select_best,
    simulate_exits_vectorized,
)
from scripts.research_s0_phase1_highlev_replay import simulate_exit


def _panel_entry(closes: np.ndarray, onboard: int = 0) -> dict[str, np.ndarray]:
    times = np.arange(len(closes), dtype="int64") * 60_000
    return {
        "open_time": times,
        "close": closes.astype("float64"),
        "vol21": np.full(len(closes), 1e8, dtype="float64"),
        "onboard": np.int64(onboard),
    }


def test_select_best_picks_strongest_long() -> None:
    closes_a = np.concatenate([np.full(10, 100.0), np.full(10, 101.0)])
    closes_b = np.concatenate([np.full(10, 100.0), np.full(10, 100.5)])
    panel = {
        "AUSDT": _panel_entry(closes_a),
        "BUSDT": _panel_entry(closes_b),
    }
    grid = [15 * 60_000]
    selected = select_best(
        panel,
        grid,
        min_ret=0.003,
        min_volume=1e6,
        allow_short=False,
        decision_bar=15,
        lookback=5,
    )
    assert len(selected) == 1
    assert selected[0][:3] == (15 * 60_000, "AUSDT", 1)
    assert selected[0][3] == pytest.approx(0.01, abs=1e-9)


def test_select_best_short_when_allowed() -> None:
    closes_a = np.full(20, 100.0)
    closes_b = np.concatenate([np.full(10, 100.0), np.full(10, 99.0)])
    panel = {
        "AUSDT": _panel_entry(closes_a),
        "BUSDT": _panel_entry(closes_b),
    }
    grid = [15 * 60_000]
    selected = select_best(
        panel,
        grid,
        min_ret=0.003,
        min_volume=1e6,
        allow_short=True,
        decision_bar=15,
        lookback=5,
    )
    assert selected[0][:3] == (15 * 60_000, "BUSDT", -1)
    assert selected[0][3] == pytest.approx(-0.01, abs=1e-9)


def test_build_trades_enters_after_signal_close(tmp_path) -> None:
    rows = []
    for index in range(100):
        rows.append(
            {
                "open_time": index * 60_000,
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 100.0,
                "quote_volume": 10_000.0,
            }
        )
    frame = pd.DataFrame(rows)
    path = tmp_path / "AUSDT.parquet"
    frame.to_parquet(path, index=False)
    panel = {
        "AUSDT": {
            "open_time": np.arange(100, dtype="int64") * 60_000,
            "close": np.full(100, 100.0),
            "vol21": np.full(100, 1e8),
            "onboard": np.int64(0),
        }
    }
    trades = build_trades(
        [(15 * 60_000, "AUSDT", 1, 0.01)],
        panel,
        tmp_path,
        entry_delay_min=0,
        decision_bar=15,
        cooldown_min=15,
        leverage=5.0,
        risk_pct=30.0,
        stop_pct=1.0,
        tp_r=2.0,
        max_hold_minutes=10,
        fee_bps=5.0,
        slippage_bps=2.0,
        maintenance_margin_pct=0.5,
    )
    assert len(trades) == 1
    # Signal closes at the minute-15 boundary; entry must be at or after minute 15's open.
    assert trades[0].entry_time >= 15 * 60_000


def test_build_panel_aligns_grid(tmp_path) -> None:
    rows = []
    for index in range(100):
        rows.append(
            {
                "open_time": index * 60_000,
                "open": 100.0 + index * 0.001,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0 + index * 0.001,
                "volume": 100.0,
                "quote_volume": 10_000.0,
            }
        )
    frame = pd.DataFrame(rows)
    path = tmp_path / "AUSDT.parquet"
    frame.to_parquet(path, index=False)
    panel, grid = build_panel(
        tmp_path,
        [{"symbol": "AUSDT", "onboard_date": 0}],
        max_alts=10,
        decision_bar=15,
        lookback=30,
    )
    assert "AUSDT" in panel
    assert len(grid) >= 5
    assert grid[0] == 15 * 60_000


def test_vectorized_exits_match_scalar() -> None:
    rng = np.random.default_rng(7)
    prices = np.cumprod(1.0 + rng.normal(0, 0.003, 200))
    rows = []
    for index, price in enumerate(prices):
        high = price * 1.004
        low = price * 0.996
        rows.append(
            {
                "open_time": index * 60_000,
                "open": float(price),
                "high": float(high),
                "low": float(low),
                "close": float(price),
                "volume": 100.0,
                "quote_volume": 10_000.0,
            }
        )
    frame = pd.DataFrame(rows)
    entries = np.array([10, 20, 30, 40, 50], dtype="int64") * 60_000
    entry_prices = frame.open.to_numpy()[[10, 20, 30, 40, 50]]
    exit_ms, exit_price, outcome, hold = simulate_exits_vectorized(
        frame, entries, entry_prices, 1, 1.0, 2.0, 10
    )
    for index in range(len(entries)):
        scalar = simulate_exit(
            frame,
            int(entries[index]),
            float(entry_prices[index]),
            1,
            1.0,
            2.0,
            10,
        )
        assert scalar is not None
        assert int(exit_ms[index]) == scalar[0]
        assert float(exit_price[index]) == pytest.approx(scalar[1])
        assert str(outcome[index]) == scalar[2]
        assert int(hold[index]) == scalar[3]
