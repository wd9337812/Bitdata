from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.benchmark_s0_ctrend import (
    candidate_passes,
    daily_symbol_features,
    fit_positive_elastic_net,
    indicator_columns,
    portfolio_returns,
    weekly_panel,
)


def test_ctrend_has_published_28_indicator_shape() -> None:
    columns = indicator_columns()

    assert len(columns) == 28
    assert len(set(columns)) == 28
    assert "rsi_14" in columns
    assert "price_sma_200" in columns
    assert "volume_macd_signal_gap" in columns
    assert "chaikin_money_flow" in columns


def test_weekly_panel_rejects_non_contiguous_forward_week() -> None:
    features = indicator_columns()
    weeks = pd.to_datetime(
        ["2025-01-05", "2025-01-12", "2025-01-26"], utc=True
    )
    frame = pd.DataFrame(
        {
            "symbol": ["AUSDT"] * 3,
            "day": weeks,
            "close": [1.0, 1.1, 1.3],
            "median_daily_volume_30d": [2_000_000.0] * 3,
            "symbol_age_days": [300.0] * 3,
            **{column: [1.0, 2.0, 3.0] for column in features},
        }
    )

    result = weekly_panel(frame)

    assert len(result) == 1
    assert np.isclose(result.iloc[0].next_return, 0.1)


def test_features_are_warmed_across_multiple_source_files(tmp_path) -> None:
    hours = pd.date_range("2024-01-01", periods=210 * 24, freq="h", tz="UTC")
    source = pd.DataFrame(
        {
            "symbol": "AUSDT",
            "open_time": hours.as_unit("ns").astype("int64") // 1_000_000,
            "open": np.linspace(1.0, 2.0, len(hours)),
            "high": np.linspace(1.01, 2.01, len(hours)),
            "low": np.linspace(0.99, 1.99, len(hours)),
            "close": np.linspace(1.0, 2.0, len(hours)),
            "quote_volume": 100_000.0,
        }
    )
    first = tmp_path / "first.parquet"
    second = tmp_path / "second.parquet"
    source.iloc[: 150 * 24].to_parquet(first, index=False)
    source.iloc[150 * 24 :].to_parquet(second, index=False)

    combined = daily_symbol_features([first, second], int(source.open_time.min()))

    assert combined.iloc[-1].price_sma_200 > 0


def test_positive_elastic_net_cannot_assign_negative_coefficients(monkeypatch) -> None:
    monkeypatch.setattr("scripts.benchmark_s0_ctrend.MINIMUM_TRAINING_ROWS", 20)
    features = [f"rank_{column}" for column in indicator_columns()]
    rows = 120
    x = np.linspace(-0.5, 0.5, rows)
    frame = pd.DataFrame(
        {
            **{feature: x for feature in features},
            "next_return": x * 0.2,
            "median_daily_volume_30d": np.full(rows, 2_000_000.0),
        }
    )

    model, alpha = fit_positive_elastic_net(frame, features)

    assert alpha > 0
    assert np.all(model.coef_ >= 0)
    assert np.count_nonzero(model.coef_) > 0


def toy_predictions() -> pd.DataFrame:
    weeks = pd.to_datetime(["2025-01-05", "2025-01-12"], utc=True)
    records = []
    for week, ranks, returns in zip(
        weeks,
        ([0.9, 0.4], [0.1, 0.6]),
        ([0.1, -0.1], [-0.1, 0.1]),
    ):
        for symbol, rank, result in zip(
            ("AUSDT", "BUSDT"), ranks, returns
        ):
            records.append(
                {
                    "week": week,
                    "symbol": symbol,
                    "score": rank,
                    "score_rank": rank,
                    "next_return": result,
                    "median_daily_volume_30d": 2_000_000.0,
                }
            )
    return pd.DataFrame(records)


def test_single_variant_charges_turnover_when_direction_flips() -> None:
    returns = portfolio_returns(toy_predictions(), 0.001, "single")

    assert np.isclose(returns.iloc[0].turnover, 1.0)
    assert np.isclose(returns.iloc[1].turnover, 2.0)
    assert np.isclose(returns.iloc[0].net_return, 0.099)
    assert np.isclose(returns.iloc[1].net_return, 0.098)


def test_candidate_requires_every_window_to_be_positive() -> None:
    report = {
        "development": {"weeks": 20, "total_return_pct": 1.0, "profit_factor": 1.1},
        "validation": {"weeks": 20, "total_return_pct": 2.0, "profit_factor": 1.2},
        "blind": {"weeks": 20, "total_return_pct": -0.1, "profit_factor": 0.9},
    }

    assert not candidate_passes(report)
