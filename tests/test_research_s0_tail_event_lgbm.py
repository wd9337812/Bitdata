import pandas as pd

from scripts.research_s0_tail_event_lgbm import (
    profit_factor,
    simulate_equity,
)


def test_profit_factor_and_equity_simulation():
    trades = pd.DataFrame(
        [
            {"symbol": "A", "entry_ms": 1_600_000_000_000, "trade_return_pct": 30.0},
            {"symbol": "B", "entry_ms": 1_600_000_000_000 + 120 * 3_600_000, "trade_return_pct": -12.0},
            {"symbol": "C", "entry_ms": 1_600_000_000_000 + 240 * 3_600_000, "trade_return_pct": 10.0},
        ]
    )

    assert profit_factor([10.0, -5.0, 5.0]) == 3.0
    result = simulate_equity(trades)
    assert result["trades"] == 3
    assert result["hard_stop_hit"] is False
    assert result["final_equity"] > 15.0
