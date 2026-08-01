from __future__ import annotations

import pandas as pd

from scripts.benchmark_s0_cross_sectional_momentum import (
    Profile,
    rank_signals,
    simulate,
    simulate_minute,
    summarize,
)


def test_rank_signal_follows_market_direction() -> None:
    rows = []
    for symbol, ret in (("BTCUSDT", 0.10), ("AUSDT", 0.20), ("BUSDT", 0.05)):
        rows.append(
            {
                "available_ms": 1,
                "symbol": symbol,
                "ret_6h": ret,
                "ret_24h": ret,
                "ret_72h": ret,
                "liquidity_24h": 100,
                "atr_24h": 1,
            }
        )
    profile = Profile("test", ("ret_24h",), 0.34, 1.0, 1.0, 1)
    result = rank_signals(pd.DataFrame(rows), profile)
    assert len(result) == 1
    assert result.loc[0, "symbol"] == "AUSDT"
    assert result.loc[0, "direction"] == "LONG"


def test_summary_uses_post_cost_net_returns() -> None:
    frame = pd.DataFrame(
        {
            "symbol": ["A", "B"],
            "direction": ["LONG", "SHORT"],
            "net_pct": [1.0, -0.5],
        }
    )
    result = summarize(frame)
    assert result["profit_factor"] == 2.0
    assert result["net_pct_points"] == 0.5


def test_simulation_enters_the_hour_after_signal_is_known() -> None:
    profile = Profile("test", ("ret_24h",), 0.1, 1.0, 1.0, 1)
    signals = pd.DataFrame(
        {
            "available_ms": [3_600_000],
            "symbol": ["AUSDT"],
            "direction": ["LONG"],
            "market_direction": ["LONG"],
            "strength": [1.0],
            "atr_24h": [1.0],
        }
    )
    panel = pd.DataFrame(
        {
            "available_ms": [3_600_000, 7_200_000],
            "symbol": ["AUSDT", "AUSDT"],
            "open": [100.0, 110.0],
            "high": [101.0, 111.0],
            "low": [99.0, 109.0],
            "close": [100.0, 110.0],
        }
    )
    result = simulate(signals, panel, profile, cost_pct=0.0)
    assert result.loc[0, "entry"] == 110.0
    assert result.loc[0, "entry_ms"] == 3_600_000


def test_simulation_can_stress_one_hour_execution_delay() -> None:
    profile = Profile("test", ("ret_24h",), 0.1, 1.0, 1.0, 1)
    signals = pd.DataFrame(
        {
            "available_ms": [3_600_000],
            "symbol": ["AUSDT"],
            "direction": ["LONG"],
            "market_direction": ["LONG"],
            "strength": [1.0],
            "atr_24h": [1.0],
        }
    )
    panel = pd.DataFrame(
        {
            "available_ms": [3_600_000, 7_200_000, 10_800_000],
            "symbol": ["AUSDT", "AUSDT", "AUSDT"],
            "open": [100.0, 110.0, 120.0],
            "high": [101.0, 111.0, 121.0],
            "low": [99.0, 109.0, 119.0],
            "close": [100.0, 110.0, 120.0],
        }
    )
    result = simulate(
        signals,
        panel,
        profile,
        cost_pct=0.0,
        execution_delay_hours=1,
    )
    assert result.loc[0, "entry"] == 120.0
    assert result.loc[0, "entry_ms"] == 7_200_000


def test_simulation_can_return_independent_overlapping_paths() -> None:
    profile = Profile("test", ("ret_24h",), 0.1, 1.0, 1.0, 2)
    signals = pd.DataFrame(
        {
            "available_ms": [0, 3_600_000],
            "symbol": ["AUSDT", "BUSDT"],
            "direction": ["LONG", "LONG"],
            "market_direction": ["LONG", "LONG"],
            "strength": [1.0, 0.9],
            "atr_24h": [1.0, 1.0],
        }
    )
    rows = []
    for symbol in ("AUSDT", "BUSDT"):
        for available_ms in (0, 3_600_000, 7_200_000, 10_800_000):
            rows.append(
                {
                    "available_ms": available_ms,
                    "symbol": symbol,
                    "open": 100.0,
                    "high": 100.5,
                    "low": 99.5,
                    "close": 100.0,
                }
            )
    panel = pd.DataFrame(rows)

    serialized = simulate(signals, panel, profile, cost_pct=0.0)
    independent = simulate(
        signals,
        panel,
        profile,
        cost_pct=0.0,
        enforce_single_position=False,
    )

    assert len(serialized) == 1
    assert len(independent) == 2


def test_minute_simulation_enters_after_configured_delay(tmp_path) -> None:
    profile = Profile("test", ("ret_24h",), 0.1, 1.0, 1.0, 1)
    signals = pd.DataFrame(
        {
            "available_ms": [60_000],
            "symbol": ["AUSDT"],
            "direction": ["LONG"],
            "market_direction": ["LONG"],
            "strength": [1.0],
            "atr_24h": [1.0],
        }
    )
    pd.DataFrame(
        {
            "open_time": [60_000, 120_000, 180_000],
            "open": [100.0, 110.0, 120.0],
            "high": [100.5, 110.5, 120.5],
            "low": [99.5, 109.5, 119.5],
            "close": [100.0, 110.0, 120.0],
        }
    ).to_parquet(tmp_path / "AUSDT.parquet", index=False)
    result = simulate_minute(signals, tmp_path, profile, 1, cost_pct=0.0)
    assert result.loc[0, "entry_ms"] == 120_000
    assert result.loc[0, "entry"] == 110.0
