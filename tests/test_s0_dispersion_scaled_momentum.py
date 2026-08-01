from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.benchmark_s0_dispersion_scaled_momentum import (
    MOMENTUM_DAYS,
    btc_volatility_exposure,
    dispersion_exposure,
    prepare_panel,
    simulate,
)


def sample_panel(days: int = 30) -> pd.DataFrame:
    records = []
    for symbol, growth in (("AAAUSDT", 1.01), ("BBBUSDT", 0.995), ("CCCUSDT", 1.002)):
        price = 100.0
        for index, day in enumerate(pd.date_range("2024-01-01", periods=days, tz="UTC")):
            records.append(
                {
                    "symbol": symbol,
                    "day": day,
                    "close": price,
                    "quote_volume": 50_000_000.0,
                    "symbol_age_days": 100 + index,
                }
            )
            price *= growth
    return pd.DataFrame(records)


def test_prepare_panel_uses_next_day_return_without_cross_symbol_leakage() -> None:
    panel = prepare_panel(sample_panel(MOMENTUM_DAYS + 5))
    aaa = panel.loc[panel.symbol.eq("AAAUSDT")].reset_index(drop=True)
    bbb = panel.loc[panel.symbol.eq("BBBUSDT")].reset_index(drop=True)

    assert np.isclose(aaa.iloc[0].next_return, 0.01)
    assert np.isclose(bbb.iloc[0].next_return, -0.005)
    assert np.isnan(aaa.iloc[-1].next_return)
    assert np.isnan(bbb.iloc[-1].next_return)
    assert np.isclose(aaa.iloc[MOMENTUM_DAYS].momentum_20d, np.log(1.01**20))


def test_dispersion_target_is_strictly_lagged() -> None:
    rows = []
    days = pd.date_range("2024-01-01", periods=MOMENTUM_DAYS + 2, tz="UTC")
    for index, day in enumerate(days):
        for symbol, value in (("A", 0.01), ("B", -0.01), ("C", 0.0)):
            rows.append(
                {
                    "day": day,
                    "symbol": symbol,
                    "return_1d": value if index < MOMENTUM_DAYS + 1 else value * 10,
                }
            )
    exposure = dispersion_exposure(pd.DataFrame(rows))

    assert np.isnan(exposure.iloc[MOMENTUM_DAYS - 1].dispersion_target)
    expected = np.median(exposure.dispersion.iloc[: MOMENTUM_DAYS + 1])
    assert np.isclose(exposure.iloc[MOMENTUM_DAYS + 1].dispersion_target, expected)
    assert exposure.iloc[MOMENTUM_DAYS + 1].raw_exposure < 1.0


def test_simulate_charges_entry_and_rotation_turnover() -> None:
    days = pd.date_range("2024-01-01", periods=2, tz="UTC")
    frame = pd.DataFrame(
        [
            {"day": days[0], "symbol": "A", "demeaned_signal": 1.0, "base_weight": 1.0, "median_quote_volume_30d": 2.0, "next_return": 0.02},
            {"day": days[0], "symbol": "B", "demeaned_signal": -0.5, "base_weight": -0.5, "median_quote_volume_30d": 1.0, "next_return": 0.0},
            {"day": days[1], "symbol": "A", "demeaned_signal": -1.0, "base_weight": -1.0, "median_quote_volume_30d": 2.0, "next_return": 0.01},
            {"day": days[1], "symbol": "B", "demeaned_signal": 0.5, "base_weight": 0.5, "median_quote_volume_30d": 1.0, "next_return": 0.0},
        ]
    )
    exposure = pd.DataFrame({"day": days, "smoothed_exposure": [1.0, 1.0]})

    daily = simulate(frame, exposure, "single_position", False, one_way_cost=0.001)

    assert np.isclose(daily.iloc[0].turnover, 1.0)
    assert np.isclose(daily.iloc[1].turnover, 2.0)
    assert np.isclose(daily.iloc[0].net_return, 0.02 - 0.001)
    assert np.isclose(daily.iloc[1].net_return, -0.01 - 0.002)


def test_btc_volatility_exposure_uses_only_btc_and_lagged_target() -> None:
    days = pd.date_range("2024-01-01", periods=55, tz="UTC")
    records = []
    for index, day in enumerate(days):
        records.append(
            {
                "day": day,
                "symbol": "BTCUSDT",
                "return_1d": 0.01 if index % 2 else -0.01,
            }
        )
        records.append(
            {"day": day, "symbol": "ALTUSDT", "return_1d": index / 10.0}
        )
    exposure = btc_volatility_exposure(pd.DataFrame(records))

    assert len(exposure) == len(days)
    assert exposure.volatility_target.iloc[:38].isna().all()
    assert exposure.volatility_target.iloc[39:].notna().all()


def test_single_long_only_never_opens_short() -> None:
    days = pd.date_range("2024-01-01", periods=2, tz="UTC")
    frame = pd.DataFrame(
        [
            {"day": days[0], "symbol": "A", "demeaned_signal": 1.0, "base_weight": 0.5, "median_quote_volume_30d": 2.0, "next_return": 0.01},
            {"day": days[0], "symbol": "B", "demeaned_signal": -1.0, "base_weight": -0.5, "median_quote_volume_30d": 1.0, "next_return": -0.02},
            {"day": days[1], "symbol": "A", "demeaned_signal": -1.0, "base_weight": -0.5, "median_quote_volume_30d": 2.0, "next_return": -0.01},
            {"day": days[1], "symbol": "B", "demeaned_signal": 1.0, "base_weight": 0.5, "median_quote_volume_30d": 1.0, "next_return": 0.02},
        ]
    )
    exposure = pd.DataFrame({"day": days, "smoothed_exposure": [1.0, 1.0]})

    daily = simulate(frame, exposure, "single_long_only", False, 0.0)

    assert daily.short_count.eq(0).all()
    assert np.isclose(daily.gross_return.sum(), 0.03)
