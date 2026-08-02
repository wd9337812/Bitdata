import pandas as pd
import pytest

from scripts.benchmark_s0_market_tsmom_protection import (
    qualifies_oos,
    simulate_protection,
)


def _signal() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "day": pd.to_datetime(["2024-01-01"], utc=True),
            "entry_day": pd.to_datetime(["2024-01-02"], utc=True),
            "exit_day": pd.to_datetime(["2024-01-03"], utc=True),
        }
    )


def test_same_hour_stop_and_take_profit_uses_stop() -> None:
    hourly = pd.DataFrame(
        {
            "open_time": [0, 1],
            "time": pd.to_datetime(["2024-01-02", "2024-01-03"], utc=True),
            "open": [100.0, 100.0],
            "high": [103.0, 100.0],
            "low": [94.0, 100.0],
            "close": [100.0, 100.0],
        }
    )

    trades = simulate_protection(_signal(), hourly, 0.02, 0.05, 0.001)

    assert trades.iloc[0].reason == "stop"
    assert trades.iloc[0].exit_price == 95.0
    assert trades.iloc[0].net_return == pytest.approx(-0.052)


def test_signal_exits_at_scheduled_open_without_trigger() -> None:
    hourly = pd.DataFrame(
        {
            "open_time": [0, 1],
            "time": pd.to_datetime(["2024-01-02", "2024-01-03"], utc=True),
            "open": [100.0, 101.0],
            "high": [101.0, 101.0],
            "low": [99.0, 101.0],
            "close": [100.5, 101.0],
        }
    )

    trades = simulate_protection(_signal(), hourly, 0.02, 0.05, 0.0)

    assert trades.iloc[0].reason == "time_exit"
    assert trades.iloc[0].exit_price == 101.0


def test_stop_only_mode_keeps_winner_until_time_exit() -> None:
    hourly = pd.DataFrame(
        {
            "open_time": [0, 1],
            "time": pd.to_datetime(["2024-01-02", "2024-01-03"], utc=True),
            "open": [100.0, 104.0],
            "high": [110.0, 104.0],
            "low": [99.0, 104.0],
            "close": [108.0, 104.0],
        }
    )

    trades = simulate_protection(_signal(), hourly, None, 0.05, 0.0)

    assert trades.iloc[0].reason == "time_exit"
    assert trades.iloc[0].net_return == pytest.approx(0.04)


def test_oos_qualification_requires_all_three_years_positive() -> None:
    annual = {year: {"net_return": 0.1} for year in ("2024", "2025", "2026")}
    report = {
        "overall": {
            "win_rate": 65.0,
            "profit_factor": 1.2,
            "max_drawdown": -0.5,
        },
        "annual": annual,
    }
    assert qualifies_oos(report)
    report["annual"]["2026"]["net_return"] = -0.01
    assert not qualifies_oos(report)
