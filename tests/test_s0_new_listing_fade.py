from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd


SCRIPT = Path(__file__).parents[1] / "scripts" / "benchmark_s0_new_listing_fade.py"
SPEC = importlib.util.spec_from_file_location("benchmark_s0_new_listing_fade", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def hourly(prices: list[float], quote_volume: float = 1_000_000.0) -> pd.DataFrame:
    start = pd.Timestamp("2024-01-01", tz="UTC")
    return pd.DataFrame(
        {
            "symbol": "NEWUSDT",
            "open_time": [
                int((start + pd.Timedelta(hours=index)).timestamp() * 1000)
                for index in range(len(prices))
            ],
            "open": prices,
            "high": [price * 1.001 for price in prices],
            "low": [price * 0.999 for price in prices],
            "close": prices,
            "quote_volume": quote_volume,
        }
    )


def test_opportunity_fades_first_72_hour_move() -> None:
    frame = hourly([100.0 + index for index in range(250)])
    result = MODULE.build_opportunity(frame)
    assert result is not None
    assert result["entry_index"] == 72
    assert result["side"] == -1
    assert result["initial_move"] > 0.70


def test_opportunity_rejects_low_liquidity() -> None:
    frame = hourly([100.0 + index for index in range(250)], quote_volume=10.0)
    assert MODULE.build_opportunity(frame) is None


def test_exit_is_stop_first_when_stop_and_take_share_a_bar() -> None:
    frame = hourly([100.0] * 250)
    opportunity = {
        "symbol": "NEWUSDT",
        "listing_time": int(frame.open_time.iloc[0]),
        "entry_time": int(frame.open_time.iloc[72]),
        "entry_index": 72,
        "initial_move": -0.05,
        "side": 1,
        "quote_volume_24h": 24_000_000.0,
    }
    frame.loc[73, "low"] = 89.0
    frame.loc[73, "high"] = 111.0
    result = MODULE.apply_exit(
        frame, opportunity, MODULE.ExitProfile("symmetric", 0.10, 0.10)
    )
    assert result["exit_reason"] == "stop"
    assert abs(result["gross_return"] + 0.10) < 1e-12


def test_metrics_deducted_returns_have_path_drawdown() -> None:
    result = MODULE.metrics(pd.Series([0.10, -0.05, 0.02]))
    assert result["trades"] == 3
    assert result["profit_factor"] == 2.4
    assert result["max_drawdown"] < 0


def test_windows_are_strictly_ordered() -> None:
    assert MODULE.WINDOWS["development_2020_2022"][1] == "2023-01-01"
    assert MODULE.WINDOWS["validation_2023"] == ("2023-01-01", "2024-01-01")
    assert MODULE.WINDOWS["test_2024"] == ("2024-01-01", "2025-01-01")
    assert MODULE.WINDOWS["blind_2025"] == ("2025-01-01", "2026-01-01")


def test_research_profiles_always_include_exchange_protection() -> None:
    protected = [profile for profile in MODULE.PROFILES if profile.name != "time_only"]
    assert protected
    assert all(profile.stop is not None for profile in protected)
