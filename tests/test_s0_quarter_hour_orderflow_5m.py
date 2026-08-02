from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.benchmark_s0_quarter_hour_orderflow_5m import (
    metrics,
    normalized_order_imbalance,
    select_trades,
)


def test_normalized_order_imbalance_uses_taker_quote_share() -> None:
    result = normalized_order_imbalance(
        pd.Series([75.0, 25.0, 1.0]), pd.Series([100.0, 100.0, 0.0])
    )
    assert result.iloc[0] == 0.5
    assert result.iloc[1] == -0.5
    assert np.isnan(result.iloc[2])


def test_select_trades_enters_after_signal_and_prevents_overlap() -> None:
    times = pd.date_range("2025-01-01", periods=4, freq="2h", tz="UTC")
    panel = pd.DataFrame(
        {
            "time": times,
            "symbol": ["A", "B", "C", "D"],
            "order_imbalance": [0.9, -0.8, 0.7, -0.6],
            "historical_q95": [0.5] * 4,
            "entry_price": [100.0] * 4,
            "exit_price": [101.0] * 4,
            "gross_forward": [0.01] * 4,
        }
    )
    trades = select_trades(panel)
    assert list(trades.symbol) == ["A", "C"]
    assert list(trades.direction) == [1, 1]


def test_metrics_deducts_round_trip_cost() -> None:
    trades = pd.DataFrame({"gross_return": [0.01, -0.005]})
    result = metrics(trades, 0.001)
    assert result["trades"] == 2
    assert result["net_return_sum"] == 0.003
    assert result["mean_net_bps"] == 15.0
