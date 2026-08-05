from __future__ import annotations

import argparse
import json

from scripts.s0_okx_pair_forward_shadow import evaluate


def _args(**kwargs) -> argparse.Namespace:
    defaults = dict(
        symbols=["BTCUSDT"],
        threshold_pct=0.20,
        z_threshold=3.0,
        window_minutes=60,
        min_samples=30,
        revert_ratio=0.5,
        stop_ratio=2.0,
        max_hold_minutes=60,
        leverage_per_leg=5.0,
        cost_bps_per_leg=7.0,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _history(now_ms: int, value: float = 0.0, count: int = 60) -> dict:
    return {
        "BTCUSDT": [
            [now_ms - (count - i) * 30_000, value]
            for i in range(count)
        ]
    }


def test_evaluate_opens_then_reverts() -> None:
    args = _args()
    now = 1_800_000_000_000
    state = {"history": _history(now, 0.0)}
    prices = {"BTCUSDT": {"binance": 100.0, "okx": 99.7}}
    events = evaluate(state, prices, now, args)
    assert len(events) == 1
    assert events[0]["type"] == "OPEN"
    assert events[0]["direction"] == -1
    assert state["open_position"] is not None
    # Premium reverts from +0.30% to +0.10% (below 0.5x target for a short).
    prices2 = {"BTCUSDT": {"binance": 100.05, "okx": 99.95}}
    events2 = evaluate(state, prices2, now + 60_000, args)
    assert len(events2) == 1
    record = events2[0]
    assert record["outcome"] == "REVERT"
    assert state["open_position"] is None
    assert "pnl_equity_pct" in record


def test_evaluate_time_exit() -> None:
    args = _args(max_hold_minutes=1)
    now = 1_800_000_000_000
    state = {"history": _history(now, 0.0)}
    prices = {"BTCUSDT": {"binance": 100.0, "okx": 99.7}}
    evaluate(state, prices, now, args)
    assert state["open_position"] is not None
    prices2 = {"BTCUSDT": {"binance": 100.0, "okx": 99.7}}
    events = evaluate(state, prices2, now + 120_000, args)
    assert events[0]["outcome"] == "TIME"
