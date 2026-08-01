from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.benchmark_s0_btc_alt_lead_lag import build_pair, metrics, simulate_long


def candle_frame(opens: list[float], closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open_time": np.arange(len(opens), dtype="int64") * 60_000,
            "open": opens,
            "close": closes,
            "trades": 10,
        }
    )


def test_build_pair_uses_next_open_to_next_open_target() -> None:
    alt = candle_frame(
        [100.0, 110.0, 121.0, 133.1, 146.41],
        [105.0, 115.0, 125.0, 135.0, 145.0],
    )
    btc = candle_frame(
        [100.0] * 5,
        [100.0, 101.0, 102.0, 103.0, 104.0],
    )

    pair = build_pair(alt, btc)

    assert len(pair) == 2
    assert np.isclose(pair.iloc[0].target, np.log(133.1 / 121.0))
    assert np.isclose(pair.iloc[1].target, np.log(146.41 / 133.1))


def test_simulate_long_charges_entry_and_exit() -> None:
    result = simulate_long(
        np.array([0.01, 0.02, -0.01]),
        np.array([0.6, 0.6, 0.4]),
        np.array([0.6, 0.4, 0.4]),
        one_way_cost=0.001,
    )

    assert np.isclose(result.net.sum(), 0.01 - 0.002)
    assert np.isclose(result.turnover.sum(), 2.0)


def test_metrics_counts_round_trips_and_drawdown() -> None:
    summary = metrics(pd.Series([0.02, -0.01, -0.01]), pd.Series([1.0, 0.0, 1.0]))

    assert summary["round_trips"] == 1
    assert summary["profit_factor"] == 1.0
    assert summary["max_drawdown_pct"] < 0
