from __future__ import annotations

import pandas as pd
import pytest

from scripts.audit_s0_bnb_fixed_gate_hourly import (
    executable_metrics,
    fixed_consensus_state,
    simulate_hourly_reentry,
)


def test_fixed_consensus_uses_frozen_fast_gate_and_positive_slow_trend() -> None:
    days = pd.date_range("2024-01-01", periods=58, tz="UTC")
    state = pd.DataFrame(
        {
            "day": days,
            "market_index": [1.0] * 56 + [1.2, 0.8],
            "universe_size": [20] * 58,
            "momentum_28d": [0.20] * 58,
        }
    )

    result = fixed_consensus_state(state, 0.15)

    assert bool(result.iloc[-2].signal)
    assert not bool(result.iloc[-1].signal)
    result.loc[result.index[-2], "momentum_28d"] = 0.14
    assert not bool(fixed_consensus_state(result, 0.15).iloc[-2].signal)


def test_hourly_simulation_reenters_on_next_daily_signal_after_stop() -> None:
    times = pd.date_range("2024-01-01", periods=97, freq="h", tz="UTC")
    hourly = pd.DataFrame(
        {
            "time": times,
            "open": [100.0] * len(times),
            "high": [101.0] * len(times),
            "low": [99.0] * len(times),
            "close": [100.0] * len(times),
        }
    )
    hourly.loc[hourly.time.eq(pd.Timestamp("2024-01-02 01:00", tz="UTC")), "low"] = 89.0
    state = pd.DataFrame(
        {
            "day": pd.to_datetime(["2024-01-01", "2024-01-02"], utc=True),
            "signal": [True, True],
        }
    )

    trades = simulate_hourly_reentry(
        hourly,
        state,
        execution_delay_hours=0,
        max_hold_hours=12,
    )

    assert len(trades) == 2
    assert trades.iloc[0].exit_reason == "stop"
    assert trades.iloc[0].exit_time == pd.Timestamp("2024-01-02 01:00", tz="UTC")
    assert trades.iloc[1].entry_time == pd.Timestamp("2024-01-03", tz="UTC")


def test_hourly_execution_delay_moves_entry_without_stale_fill() -> None:
    times = pd.date_range("2024-01-01", periods=72, freq="h", tz="UTC")
    hourly = pd.DataFrame(
        {
            "time": times,
            "open": range(100, 172),
            "high": range(101, 173),
            "low": range(99, 171),
            "close": range(100, 172),
        }
    )
    state = pd.DataFrame(
        {
            "day": pd.to_datetime(["2024-01-01"], utc=True),
            "signal": [True],
        }
    )

    trades = simulate_hourly_reentry(
        hourly,
        state,
        execution_delay_hours=4,
        max_hold_hours=12,
    )

    assert trades.iloc[0].entry_time == pd.Timestamp("2024-01-02 04:00", tz="UTC")
    assert trades.iloc[0].entry_price == pytest.approx(128.0)


def test_short_path_uses_high_for_stop_and_inverts_return() -> None:
    times = pd.date_range("2024-01-02", periods=3, freq="h", tz="UTC")
    hourly = pd.DataFrame(
        {
            "time": times,
            "open": [100.0, 100.0, 115.0],
            "high": [101.0, 116.0, 115.0],
            "low": [99.0, 98.0, 114.0],
            "close": [100.0, 115.0, 114.0],
        }
    )
    state = pd.DataFrame(
        {
            "day": pd.to_datetime(["2024-01-01"], utc=True),
            "signal": [True],
        }
    )

    trades = simulate_hourly_reentry(
        hourly,
        state,
        execution_delay_hours=0,
        stop_pct=0.15,
        max_hold_hours=2,
        direction=-1,
    )

    assert len(trades) == 1
    assert trades.iloc[0].direction == -1
    assert trades.iloc[0].exit_reason == "stop"
    assert trades.iloc[0].exit_price == pytest.approx(115.0)
    assert trades.iloc[0].gross_return == pytest.approx(-0.15)


def test_invalid_direction_is_rejected() -> None:
    with pytest.raises(ValueError, match="direction"):
        simulate_hourly_reentry(
            pd.DataFrame(),
            pd.DataFrame(),
            execution_delay_hours=0,
            direction=0,
        )


def test_executable_metrics_apply_contract_rounding_cost_and_hard_stop_headroom() -> None:
    trades = pd.DataFrame(
        {
            "entry_time": pd.to_datetime(["2024-01-01", "2024-01-02"], utc=True),
            "entry_price": [600.0, 600.0],
            "initial_stop_pct": [0.10, 0.10],
            "gross_return": [0.10, -0.10],
        }
    )

    result = executable_metrics(
        trades,
        one_way_cost=0.001,
        starting_equity=15.0,
    )

    assert result["executed"] == 2
    assert result["cost_usdt"] > 0
    assert result["ending_equity"] > 5.0
    assert result["hard_stopped"] is False
