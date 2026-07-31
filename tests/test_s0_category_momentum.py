from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.benchmark_s0_category_momentum import (
    candidate_passes,
    category_signals,
    choose_positions,
    daily_symbol_market,
)


def test_daily_market_rejects_calendar_gaps(tmp_path) -> None:
    days = pd.to_datetime(["2025-01-01", "2025-01-02", "2025-01-04"], utc=True)
    source = pd.DataFrame(
        {
            "symbol": "AUSDT",
            "open_time": days.as_unit("ns").astype("int64") // 1_000_000,
            "open": [1.0, 2.0, 4.0],
            "high": [1.0, 2.0, 4.0],
            "low": [1.0, 2.0, 4.0],
            "close": [1.0, 2.0, 4.0],
            "quote_volume": 2_000_000.0,
        }
    )
    path = tmp_path / "AUSDT.parquet"
    source.to_parquet(path, index=False)

    result = daily_symbol_market(path, int(source.open_time.min()))

    assert np.isclose(result.iloc[0].next_return, 1.0)
    assert np.isnan(result.iloc[1].next_return)


def signal_frame() -> pd.DataFrame:
    day = pd.Timestamp("2025-01-01", tz="UTC")
    return pd.DataFrame(
        {
            "day": [day] * 4,
            "symbol": ["AUSDT", "BUSDT", "CUSDT", "DUSDT"],
            "cluster": [0, 0, 1, 1],
            "formation_return": [0.2, 0.1, -0.1, -0.2],
            "next_return": [0.01, 0.02, -0.01, -0.02],
            "median_volume_30d": [4.0, 1.0, 1.0, 4.0],
        }
    )


def test_category_signal_uses_liquidity_weighted_constituents() -> None:
    result = category_signals(signal_frame())
    cluster_zero = result.loc[result.cluster.eq(0)].iloc[0]

    assert np.isclose(cluster_zero.category_signal, (0.2 * 2 + 0.1) / 3)
    assert cluster_zero.category_rank == 1.0


def test_single_uses_liquid_representative_of_strongest_category() -> None:
    signals = category_signals(signal_frame())
    selected = choose_positions(signals, "single")

    assert len(selected) == 1
    assert selected.iloc[0].symbol == "AUSDT"
    assert selected.iloc[0].position == 1.0


def test_top3_returns_representatives_after_merge_reindexes_rows() -> None:
    signals = pd.concat(
        [
            category_signals(signal_frame()),
            category_signals(
                signal_frame().assign(
                    day=pd.Timestamp("2025-01-02", tz="UTC")
                )
            ),
        ],
        ignore_index=True,
    )

    selected = choose_positions(signals, "top3")

    assert len(selected) == 4
    assert selected.groupby("day").position.apply(lambda value: value.abs().sum()).eq(1.0).all()


def test_candidate_requires_every_window() -> None:
    report = {
        "development": {"days": 150, "total_return_pct": 1.0, "profit_factor": 1.1},
        "validation": {"days": 150, "total_return_pct": 1.0, "profit_factor": 1.1},
        "blind": {"days": 150, "total_return_pct": -1.0, "profit_factor": 0.9},
    }

    assert not candidate_passes(report)
