from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.validate_s0_volume_weighted_tsmom_2026 import (
    block_bootstrap_mean,
    with_cost,
)


def test_block_bootstrap_detects_consistently_positive_series() -> None:
    result = block_bootstrap_mean(
        pd.Series([0.01] * 30), iterations=200, seed=7
    )

    assert result["ci_low_daily_pct"] > 0
    assert result["probability_mean_positive_pct"] == 100.0


def test_with_cost_reprices_turnover_without_changing_gross() -> None:
    daily = pd.DataFrame(
        {"gross_return": [0.02], "turnover": [1.5], "net_return": [0.0]}
    )
    result = with_cost(daily, 0.001)

    assert np.isclose(result.iloc[0].gross_return, 0.02)
    assert np.isclose(result.iloc[0].net_return, 0.0185)
