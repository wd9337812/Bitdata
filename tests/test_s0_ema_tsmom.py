from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.benchmark_s0_ema_tsmom import (
    composite_signal,
    paper_ema,
    portfolio_daily,
)


def test_paper_ema_uses_exp_decay() -> None:
    series = pd.Series([1.0, 2.0, 3.0, 4.0])
    result = paper_ema(series, 2)
    alpha = 1.0 - np.exp(-0.5)

    expected = 1.0 + alpha * (2.0 - 1.0)
    assert np.isclose(result.iloc[1], expected)


def test_composite_signal_has_expected_direction_for_clear_trend() -> None:
    rising = pd.Series(np.linspace(1.0, 10.0, 400))
    falling = pd.Series(np.linspace(10.0, 1.0, 400))

    assert composite_signal(rising).dropna().iloc[-1] > 0
    assert composite_signal(falling).dropna().iloc[-1] < 0


def test_portfolio_daily_charges_weight_turnover() -> None:
    panel = pd.DataFrame(
        {
            "day": pd.to_datetime(["2025-01-01", "2025-01-02"], utc=True),
            "symbol": ["BTCUSDT", "BTCUSDT"],
            "signal": [1.0, -1.0],
            "bounded_signal": [1.0, -1.0],
            "available_days": [223, 224],
            "next_return": [0.08, -0.08],
        }
    )

    result = portfolio_daily(panel, "sign")

    assert np.isclose(result.iloc[0].turnover, 0.125)
    assert np.isclose(result.iloc[1].turnover, 0.25)
    assert result.net_return.gt(0).all()
