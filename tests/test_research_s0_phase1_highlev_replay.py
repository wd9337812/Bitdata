from __future__ import annotations

import pandas as pd
import pytest

from scripts.research_s0_phase1_highlev_replay import (
    build_trades,
    generate_candidates,
    simulate_equity,
    simulate_exit,
)


def _minute_frame(prices: list[float], start_ms: int = 0) -> pd.DataFrame:
    rows = []
    for index, price in enumerate(prices):
        rows.append(
            {
                "open_time": start_ms + index * 60_000,
                "open": float(price),
                "high": float(price) * 1.001,
                "low": float(price) * 0.999,
                "close": float(price),
                "volume": 100.0,
            }
        )
    return pd.DataFrame(rows)


def test_generate_candidates_enters_at_next_1m_open() -> None:
    # 15m decision bars; a breakout occurs on the fourth 15m bar.
    prices = []
    for bar in range(12):
        base = 100.0 + bar * 1.0
        prices.extend([base] * 15)
    frame = _minute_frame(prices)
    # Force the seventh 15m bar close to break the prior 5-bar high.
    frame.loc[104, "close"] = 120.0
    candidates = generate_candidates(
        frame,
        decision_minutes=15,
        lookback=5,
        rule="breakout",
        min_gap_bars=4,
        allow_short=False,
        vol_z_threshold=2.0,
    )
    assert len(candidates) >= 1
    signal_ms, direction, _bar, entry_ms = candidates[0]
    assert direction == 1
    assert signal_ms == 105 * 60_000
    assert entry_ms == 105 * 60_000


def test_simulate_exit_is_adverse_first() -> None:
    frame = _minute_frame([100.0, 100.0, 100.0], start_ms=0)
    # Bar 1: low touches stop (98) and high touches target (104).
    frame.loc[1, "low"] = 97.0
    frame.loc[1, "high"] = 105.0
    result = simulate_exit(
        frame,
        entry_ms=0,
        entry_price=100.0,
        direction=1,
        stop_pct=2.0,
        tp_r=2.0,
        max_hold_minutes=5,
    )
    assert result is not None
    exit_ms, exit_price, outcome, hold = result
    assert outcome == "STOP"
    assert exit_price == 98.0
    assert exit_ms == 60_000


def test_build_trades_applies_leverage_and_costs() -> None:
    frame = _minute_frame([100.0, 100.0, 100.0], start_ms=0)
    frame.attrs["symbol"] = "TESTUSDT"
    # Target at 102 on bar 1.
    frame.loc[1, "high"] = 102.5
    trades = build_trades(
        frame,
        candidates=[(15 * 60_000, 1, 0, 0)],
        leverage=5.0,
        risk_pct=30.0,
        stop_pct=2.0,
        tp_r=1.0,
        max_hold_minutes=10,
        fee_bps=5.0,
        slippage_bps=2.0,
        maintenance_margin_pct=0.5,
    )
    assert len(trades) == 1
    trade = trades[0]
    assert trade.outcome == "TAKE_PROFIT"
    # 2% move * 5x = 10% equity; costs = (5+2)*2*5/100 = 0.7%.
    assert trade.pnl_equity_pct == pytest.approx(9.3, abs=1e-9)
    assert trade.symbol == "TESTUSDT"


def test_simulate_equity_takes_one_position_at_a_time() -> None:
    from scripts.research_s0_phase1_highlev_replay import CandidateTrade

    trades = [
        CandidateTrade("A", 0, 10_000, 1, 100.0, 102.0, "TAKE_PROFIT", 10.0, 10),
        CandidateTrade("B", 5_000, 20_000, 1, 100.0, 98.0, "STOP", -10.7, 5),
        CandidateTrade("C", 11_000, 12_000, 1, 100.0, 102.0, "TAKE_PROFIT", 10.0, 2),
    ]
    curve, taken = simulate_equity(trades, initial_equity=100.0, max_trades=0)
    assert [trade.symbol for trade in taken] == ["A", "C"]
    assert curve.equity.iloc[-1] == pytest.approx(100.0 * 1.10 * 1.10, abs=1e-9)


def test_liquidation_path_when_stop_beyond_maintenance() -> None:
    frame = _minute_frame([100.0, 100.0], start_ms=0)
    frame.attrs["symbol"] = "TESTUSDT"
    trades = build_trades(
        frame,
        candidates=[(0, 1, 0, 0)],
        leverage=20.0,
        risk_pct=150.0,
        stop_pct=10.0,
        tp_r=1.0,
        max_hold_minutes=5,
        fee_bps=5.0,
        slippage_bps=2.0,
        maintenance_margin_pct=0.5,
    )
    assert len(trades) == 1
    assert trades[0].outcome == "LIQUIDATED"
    assert trades[0].pnl_equity_pct == -100.0
