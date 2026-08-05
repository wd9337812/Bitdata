from __future__ import annotations

import argparse

import numpy as np
import pandas as pd
import pytest

from scripts.benchmark_s0_30d_momentum_highlev import (
    ExitProfile,
    _simulate_minute_exit,
    _stop_distance,
    account_metrics,
    account_trade,
    paper_trades,
    simulate_account,
)


def _minute_frame(prices: list[float], start_ms: int = 0) -> pd.DataFrame:
    rows = []
    for index, price in enumerate(prices):
        rows.append(
            {
                "open_time": start_ms + index * 60_000,
                "open": float(price),
                "high": float(price),
                "low": float(price),
                "close": float(price),
            }
        )
    return pd.DataFrame(rows)


def _args(**kwargs) -> argparse.Namespace:
    defaults = dict(
        cost_pct=0.60,
        leverage=5.0,
        risk_pct=30.0,
        initial_equity=10.0,
        hard_stop_equity=5.0,
        margin_fraction=0.90,
        maintenance_margin_pct=0.5,
        max_trades=0,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_stop_distance_fixed_and_atr() -> None:
    fixed = ExitProfile("fixed", None, 3.0, None, None, None, 24)
    assert _stop_distance(100.0, 2.0, fixed) == pytest.approx(3.0)
    atr = ExitProfile("atr", 2.5, None, None, None, None, 120, max_stop_pct=12.0)
    assert _stop_distance(100.0, 2.0, atr) == pytest.approx(5.0)
    # ATR stop is capped at max_stop_pct.
    assert _stop_distance(100.0, 10.0, atr) == pytest.approx(12.0)


def test_minute_exit_adverse_first_stop() -> None:
    frame = _minute_frame([100.0, 100.0, 100.0])
    frame.loc[1, "low"] = 97.0
    frame.loc[1, "high"] = 105.0
    profile = ExitProfile("t", 2.5, None, 2.5, None, None, 120)
    result = _simulate_minute_exit(
        frame, 0, 100.0, 1, 2.0, 2.0, profile
    )
    assert result is not None
    exit_ms, exit_price, outcome, hold = result
    assert outcome == "STOP"
    assert exit_price == 98.0
    assert exit_ms == 60_000


def test_minute_exit_take_profit() -> None:
    frame = _minute_frame([100.0, 100.0, 100.0])
    frame.loc[1, "high"] = 102.5
    profile = ExitProfile("t", 2.5, None, 1.0, None, None, 120)
    result = _simulate_minute_exit(
        frame, 0, 100.0, 1, 2.0, 2.0, profile
    )
    assert result is not None
    assert result[2] == "TAKE"
    assert result[1] == 102.0


def test_minute_exit_trailing_arms_and_exits() -> None:
    frame = _minute_frame([100.0, 103.0, 100.5, 100.0])
    profile = ExitProfile("t", 2.5, None, None, 1.0, 1.0, 48)
    result = _simulate_minute_exit(
        frame, 0, 100.0, 1, 2.0, 2.0, profile
    )
    assert result is not None
    exit_ms, exit_price, outcome, hold = result
    assert outcome == "TRAIL"
    assert exit_price == 101.0
    assert exit_ms == 2 * 60_000


def test_account_trade_liquidation_and_normal() -> None:
    frame = _minute_frame([100.0, 105.0, 101.0, 100.0])
    profile = ExitProfile("t", None, 3.0, None, 1.0, 1.0, 1)
    liquidated = account_trade(
        frame, 0, 100.0, 1, 2.0, profile, 9.6, 10.0, 0.60, 9.5
    )
    assert liquidated["outcome"] == "LIQUIDATED"
    assert liquidated["pnl_equity_pct"] == -100.0
    normal = account_trade(
        frame, 0, 100.0, 1, 2.0, profile, 3.0, 5.0, 0.60, 19.5
    )
    # Long hits +5% trigger at bar 1, trails at 102 on bar 2.
    assert normal["outcome"] == "TRAIL"
    gross = (102.0 / 100.0 - 1.0) * 100.0 * 5.0
    assert normal["pnl_equity_pct"] == pytest.approx(gross - 0.60 * 5.0)


def test_paper_trades_and_account_sim_with_tmp_path(tmp_path) -> None:
    import pyarrow.parquet as pq

    frame = _minute_frame([100.0] * 10)
    frame.loc[1, "high"] = 104.0
    frame.loc[2, "low"] = 100.5
    pq.write_table(pyarrow_table_from(frame), tmp_path / "TESTUSDT.parquet")
    signals = pd.DataFrame(
        [
            {
                "symbol": "TESTUSDT",
                "available_ms": 0,
                "direction": "LONG",
                "market_direction": "LONG",
                "strength": 0.9,
                "atr_24h": 2.0,
            }
        ]
    )
    profile = ExitProfile("t", None, 3.0, None, 1.0, 1.0, 1)
    paper = paper_trades(signals, tmp_path, profile, 0, 0.60)
    assert len(paper) == 1
    assert paper.iloc[0]["symbol"] == "TESTUSDT"
    assert paper.iloc[0]["entry_ms"] == 0
    assert paper.iloc[0]["gross_pct"] == pytest.approx(1.0, abs=1e-9)
    assert paper.iloc[0]["net_pct"] == pytest.approx(0.4, abs=1e-9)

    rows, curve = simulate_account(
        paper, tmp_path, profile, _args(leverage=5.0)
    )
    assert len(rows) == 1
    assert curve.equity.iloc[-1] == pytest.approx(
        10.0 * (1.0 + rows.iloc[0]["pnl_equity_pct"] / 100.0),
        abs=1e-9,
    )


def test_account_metrics_reach_and_ruin() -> None:
    args = _args(initial_equity=10.0, hard_stop_equity=5.0)
    rows = pd.DataFrame(
        [
            {
                "symbol": "AUSDT",
                "outcome": "TAKE",
                "pnl_equity_pct": 500.0,
            }
        ]
    )
    curve = pd.DataFrame(
        [
            {"time": 0, "equity": 10.0, "trade_id": 0, "symbol": ""},
            {"time": 1000, "equity": 60.0, "trade_id": 1, "symbol": "AUSDT"},
        ]
    )
    metrics = account_metrics(rows, curve, args)
    assert metrics["reached_10000u"] is False  # 60U is not 1000x.
    assert metrics["multiplier"] == pytest.approx(6.0)


def pyarrow_table_from(frame: pd.DataFrame):
    import pyarrow as pa

    return pa.Table.from_pandas(frame)
