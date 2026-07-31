from __future__ import annotations

import pandas as pd

from scripts.validate_s0_slow_momentum_oos_2025 import (
    concentration_metrics,
    cost_adjusted,
    qualifies,
)


def test_cost_adjusted_recomputes_net_return() -> None:
    trades = pd.DataFrame({"gross_pct": [1.0, -0.5], "net_pct": [0.0, 0.0]})

    adjusted = cost_adjusted(trades, 0.24)

    assert adjusted.net_pct.tolist() == [0.76, -0.74]
    assert adjusted.cost_pct.tolist() == [0.24, 0.24]


def test_concentration_reports_result_without_best_symbol() -> None:
    trades = pd.DataFrame(
        {
            "symbol": ["AAAUSDT", "BBBUSDT", "CCCUSDT"],
            "net_pct": [3.0, 2.0, -1.0],
        }
    )

    result = concentration_metrics(trades)

    assert result["net_without_best_symbol_pct_points"] == 1.0
    assert result["top_five_share_of_positive_pct"] == 100.0


def test_qualification_requires_delayed_execution_to_remain_positive() -> None:
    months = {
        f"2025-{month:02d}": {"net_pct_points": 1.0}
        for month in range(7, 13)
    }
    scenarios = {
        "delay_0h_cost_0.12pct": {
            "overall": {"trades": 40, "profit_factor": 1.2},
            "monthly": months,
            "concentration": {"net_without_best_symbol_pct_points": 2.0},
        },
        "delay_0h_cost_0.24pct": {
            "overall": {"profit_factor": 1.1},
        },
        "delay_1h_cost_0.12pct": {
            "overall": {"profit_factor": 0.9},
        },
    }

    assert not qualifies(scenarios)
