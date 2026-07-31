from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.benchmark_s0_long_only_momentum import (
    candidate_passes,
    regime_mask,
    select_positions,
    strategy_returns,
)


def toy_weekly() -> pd.DataFrame:
    days = pd.to_datetime(["2025-01-05", "2025-01-12"], utc=True)
    records = []
    for day, returns in zip(days, ([0.1, 0.02], [-0.1, 0.03])):
        for symbol, rank, result in zip(
            ("AUSDT", "BUSDT"), (1.0, 0.5), returns
        ):
            records.append(
                {
                    "day": day,
                    "symbol": symbol,
                    "momentum_rank": rank,
                    "median_volume_30d": 2_000_000.0,
                    "forward_return": result,
                    "btc_above_sma_100": True,
                    "btc_formation_return": 0.1,
                    "market_breadth": 0.05,
                }
            )
    return pd.DataFrame(records)


def test_btc_breadth_regime_requires_all_three_conditions() -> None:
    frame = toy_weekly().iloc[:1].copy()
    assert regime_mask(frame, "btc_breadth").iloc[0]

    frame.loc[:, "market_breadth"] = -0.01
    assert not regime_mask(frame, "btc_breadth").iloc[0]


def test_single_selects_only_top_winner() -> None:
    selected = select_positions(toy_weekly(), "single", "unfiltered")

    assert selected.symbol.tolist() == ["AUSDT", "AUSDT"]
    assert selected.position.tolist() == [1.0, 1.0]


def test_single_charges_turnover_when_winner_is_reselected() -> None:
    returns = strategy_returns(toy_weekly(), "single", "unfiltered", 0.001)

    assert np.isclose(returns.iloc[0].turnover, 1.0)
    assert np.isclose(returns.iloc[1].turnover, 0.0)
    assert np.isclose(returns.iloc[0].net_return, 0.099)
    assert np.isclose(returns.iloc[1].net_return, -0.1)


def test_candidate_requires_each_window_and_enough_active_weeks() -> None:
    report = {
        "development": {
            "active_weeks": 10,
            "total_return_pct": 1.0,
            "profit_factor": 1.1,
        },
        "blind": {
            "active_weeks": 7,
            "total_return_pct": 1.0,
            "profit_factor": 1.1,
        },
    }

    assert not candidate_passes(report)
