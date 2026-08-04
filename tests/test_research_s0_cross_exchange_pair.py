from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts.research_s0_cross_exchange_pair import (
    build_pair_trades,
    simulate_premium_reverts_vectorized,
)


def _pair_frame() -> pd.DataFrame:
    rows = []
    for index in range(10):
        rows.append(
            {
                "open_time": index * 60_000,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "bybit_open": 100.0,
                "bybit_close": 100.0,
                "premium_pct": 0.2,
                "vol21": 1e8,
            }
        )
    frame = pd.DataFrame(rows)
    # Minute 6: premium reverts to 0.1 (below target for a short).
    frame.loc[6, "premium_pct"] = 0.1
    frame.loc[6, "close"] = 101.0
    frame.loc[6, "bybit_close"] = 99.0
    return frame


def test_vectorized_revert_exit_matches_semantics() -> None:
    frame = _pair_frame()
    exit_idx, outcome, hold, _ = simulate_premium_reverts_vectorized(
        frame,
        np.array([5 * 60_000], dtype="int64"),
        np.array([0.2], dtype="float64"),
        direction=-1,
        revert_ratio=0.5,
        stop_ratio=2.0,
        max_hold=10,
    )
    assert exit_idx[0] == 6
    assert outcome[0] == "REVERT"
    assert hold[0] == 2


def test_pair_pnl_uses_both_legs_and_costs() -> None:
    frame = _pair_frame()
    premium = {"TESTUSDT": frame}
    events = [(5 * 60_000, "TESTUSDT", -1, 0.2)]
    trades = build_pair_trades(
        events,
        premium,
        delay=0,
        cooldown_min=30,
        leverage_per_leg=5.0,
        revert_ratio=0.5,
        stop_ratio=2.0,
        max_hold=10,
        binance_fee_bps=5.0,
        binance_slippage_bps=2.0,
        bybit_fee_bps=5.5,
        bybit_slippage_bps=2.0,
        min_entry_premium_pct=0.05,
        max_entry_premium_pct=999.0,
        bybit_entry_delay_min=0,
        cost_multiplier=1.0,
    )
    assert len(trades) == 1
    trade = trades[0]
    assert trade.outcome == "REVERT"
    # Signal at minute 6 close; execution at minute 7 open (both legs 100):
    # pair return 0 -> equity pct = -(14.5/10000 * 5/2 * 100) = -0.3625.
    assert trade.pnl_equity_pct == pytest.approx(-0.3625, abs=1e-9)


def test_pair_trade_skips_missing_bybit_price(tmp_path) -> None:
    frame = _pair_frame()
    frame.loc[5, "bybit_open"] = np.nan
    premium = {"TESTUSDT": frame}
    trades = build_pair_trades(
        [(5 * 60_000, "TESTUSDT", -1, 0.2)],
        premium,
        delay=0,
        cooldown_min=30,
        leverage_per_leg=5.0,
        revert_ratio=0.5,
        stop_ratio=2.0,
        max_hold=10,
        binance_fee_bps=5.0,
        binance_slippage_bps=2.0,
        bybit_fee_bps=5.5,
        bybit_slippage_bps=2.0,
        min_entry_premium_pct=0.05,
        max_entry_premium_pct=999.0,
        bybit_entry_delay_min=0,
        cost_multiplier=1.0,
    )
    assert trades == []
