from __future__ import annotations

import pandas as pd
import pytest

from scripts.benchmark_s0_adaptive_trend import (
    Parameters,
    add_indicators,
    concentration_robustness,
    select_s0_trades,
    simulate_direction,
)


def sample_frame(closes: list[float]) -> pd.DataFrame:
    times = pd.date_range("2025-01-01", periods=len(closes), freq="6h", tz="UTC")
    return pd.DataFrame(
        {
            "time": times,
            "symbol": "TESTUSDT",
            "open": closes,
            "high": [value + 0.5 for value in closes],
            "low": [value - 0.5 for value in closes],
            "close": closes,
            "quote_volume": 1_000_000.0,
            "hourly_rows": 6,
        }
    )


def test_entry_signal_is_shifted_to_next_bar() -> None:
    frame = sample_frame([100, 100, 100, 110, 111, 112])
    parameters = Parameters(lookback=2, threshold=0.05, atr_multiplier=2, atr_period=2)
    indicators = add_indicators(frame, parameters)
    assert indicators.loc[3, "signal_momentum"] == 0.0
    assert indicators.loc[4, "signal_momentum"] == pytest.approx(0.10)


def test_long_stop_uses_adverse_opening_gap() -> None:
    frame = sample_frame([100, 100, 110, 111, 112, 113])
    frame.loc[4, ["open", "high", "low", "close"]] = [90, 91, 89, 90]
    parameters = Parameters(lookback=1, threshold=0.05, atr_multiplier=1, atr_period=2)
    _, trades = simulate_direction(frame, parameters, "LONG", one_way_cost=0.0)
    assert len(trades) == 1
    assert trades.iloc[0].exit_reason == "trailing_stop"
    assert trades.iloc[0].gross_return < -0.15


def test_s0_selection_never_overlaps_positions() -> None:
    utc = "UTC"
    trades = pd.DataFrame(
        [
            {
                "symbol": "AAAUSDT",
                "direction": "LONG",
                "parameters": "p",
                "entry_time": pd.Timestamp("2025-02-01", tz=utc),
                "exit_time": pd.Timestamp("2025-02-03", tz=utc),
                "net_return": 0.1,
            },
            {
                "symbol": "BBBUSDT",
                "direction": "LONG",
                "parameters": "p",
                "entry_time": pd.Timestamp("2025-02-02", tz=utc),
                "exit_time": pd.Timestamp("2025-02-04", tz=utc),
                "net_return": 0.2,
            },
        ]
    )
    selection = pd.DataFrame(
        [
            {
                "month": "2025-02",
                "symbol": "AAAUSDT",
                "direction": "LONG",
                "parameters": "p",
                "prior_sharpe": 2.0,
                "prior_trades": 3,
            },
            {
                "month": "2025-02",
                "symbol": "BBBUSDT",
                "direction": "LONG",
                "parameters": "p",
                "prior_sharpe": 3.0,
                "prior_trades": 3,
            },
        ]
    )
    chosen = select_s0_trades(trades, selection)
    assert chosen.symbol.tolist() == ["AAAUSDT"]


def test_concentration_audit_removes_best_trade() -> None:
    trades = pd.DataFrame(
        {
            "symbol": ["A", "B"],
            "entry_time": pd.to_datetime(
                ["2026-02-01", "2026-03-01"], utc=True
            ),
            "net_return": [1.0, -0.2],
        }
    )
    result = concentration_robustness(trades)["final_blind_2026_h1"]
    assert result["best_trade"]["symbol"] == "A"
    assert result["without_best_trade"]["net_return"] == -0.2
