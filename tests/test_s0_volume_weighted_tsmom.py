from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.benchmark_s0_volume_weighted_tsmom import (
    FORMATION_DAYS,
    build_daily_panel,
    candidate_passes,
    sparse_balanced_daily,
    top_k_daily,
    volume_side_weights,
    weighted_portfolio_daily,
)


def test_volume_side_weights_are_dollar_neutral() -> None:
    frame = pd.DataFrame(
        {
            "formation_1d": [0.1, 0.2, -0.1, -0.2],
            "quote_volume": [10.0, 30.0, 20.0, 20.0],
        }
    )
    weights = volume_side_weights(frame, 1)

    assert np.isclose(weights[0], 0.25)
    assert np.isclose(weights[1], 0.75)
    assert np.isclose(weights[2], -0.5)
    assert np.isclose(weights[3], -0.5)
    assert np.isclose(weights.sum(), 0.0)
    assert np.isclose(weights.abs().sum(), 2.0)


def toy_panel() -> pd.DataFrame:
    days = pd.to_datetime(["2025-01-01", "2025-01-02"], utc=True)
    return pd.DataFrame(
        {
            "day": [days[0], days[0], days[1], days[1]],
            "symbol": ["AUSDT", "BUSDT", "AUSDT", "BUSDT"],
            "quote_volume": [20_000_000.0] * 4,
            "median_quote_volume_30d": [20_000_000.0] * 4,
            "symbol_age_days": [40.0] * 4,
            "formation_1d": [0.1, -0.1, -0.1, 0.1],
            "next_return": [0.02, -0.02, -0.02, 0.02],
        }
    )


def test_weighted_portfolio_charges_actual_turnover() -> None:
    daily = weighted_portfolio_daily(
        toy_panel(), 1, one_way_cost=0.001, gross_exposure=2.0
    )

    assert np.isclose(daily.iloc[0].turnover, 2.0)
    assert np.isclose(daily.iloc[1].turnover, 4.0)
    assert np.isclose(daily.iloc[0].gross_return, 0.04)
    assert np.isclose(daily.iloc[1].gross_return, 0.04)
    assert np.isclose(daily.iloc[0].net_return, 0.038)
    assert np.isclose(daily.iloc[1].net_return, 0.036)


def test_top_k_single_selects_highest_volume_and_direction() -> None:
    panel = toy_panel()
    panel.loc[panel.symbol.eq("AUSDT"), "quote_volume"] = 30_000_000.0
    daily = top_k_daily(panel, 1, 1, one_way_cost=0.0)

    assert daily.symbol.tolist() == ["AUSDT", "AUSDT"]
    assert daily.direction.tolist() == [1.0, -1.0]
    assert np.allclose(daily.gross_return, [0.02, 0.02])


def test_candidate_must_pass_every_window() -> None:
    report = {
        "development": {"total_return_pct": 1.0, "daily_profit_factor": 1.1},
        "validation": {"total_return_pct": 2.0, "daily_profit_factor": 1.2},
        "test": {"total_return_pct": -0.1, "daily_profit_factor": 0.9},
    }

    assert not candidate_passes(report)


def test_sparse_balanced_selects_each_side_and_keeps_gross_one() -> None:
    panel = toy_panel()
    daily = sparse_balanced_daily(panel, 1, 1, one_way_cost=0.0)

    assert daily.long_count.tolist() == [1, 1]
    assert daily.short_count.tolist() == [1, 1]
    assert np.allclose(daily.gross_return, [0.02, 0.02])
    assert np.isclose(daily.iloc[0].turnover, 1.0)
    assert np.isclose(daily.iloc[1].turnover, 2.0)


def test_formation_and_forward_returns_reject_calendar_gaps(
    tmp_path, monkeypatch
) -> None:
    days = pd.to_datetime(
        ["2025-01-01", "2025-01-02", "2025-01-04"], utc=True
    )
    source = pd.DataFrame(
        {
            "symbol": ["AUSDT"] * 3,
            "day": days,
            "close": [1.0, 2.0, 4.0],
            "quote_volume": [2_000_000.0] * 3,
            "next_return": [1.0, 1.0, np.nan],
            "symbol_age_days": [40.0] * 3,
        }
    )
    monkeypatch.setattr(
        "scripts.benchmark_s0_volume_weighted_tsmom.load_manifest",
        lambda _: ({"AUSDT": 0}, {"source": "test"}),
    )
    monkeypatch.setattr(
        "scripts.benchmark_s0_volume_weighted_tsmom.daily_symbol_frame",
        lambda *_: source.copy(),
    )
    (tmp_path / "parquet").mkdir()
    (tmp_path / "parquet" / "AUSDT.parquet").touch()

    panel, _ = build_daily_panel(tmp_path)

    assert np.isclose(panel.iloc[0].next_return, 1.0)
    assert np.isnan(panel.iloc[1].next_return)
    assert np.isnan(panel.iloc[2].next_return)
    assert np.isclose(panel.iloc[1].formation_1d, 1.0)
    assert np.isnan(panel.iloc[2].formation_1d)
    assert set(FORMATION_DAYS) == {1, 7, 14}
