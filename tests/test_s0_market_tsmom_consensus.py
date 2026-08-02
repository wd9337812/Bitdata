import pandas as pd

from scripts.benchmark_s0_market_tsmom_consensus import (
    consensus_state,
    qualifies_oos,
)


def test_consensus_requires_fast_and_slow_trend() -> None:
    state = pd.DataFrame(
        {
            "market_index": [1.0] * 56 + [1.2, 0.8],
            "signal": [False] * 56 + [True, True],
        }
    )

    result = consensus_state(state, 56)

    assert bool(result.iloc[-2].signal)
    assert not bool(result.iloc[-1].signal)


def test_oos_requires_every_frozen_year_positive() -> None:
    annual = {
        year: {"net_return": 0.1, "profit_factor": 1.2}
        for year in ("2024", "2025", "2026")
    }
    result = {
        "oos_stress": {
            "overall": {"profit_factor": 1.2, "max_drawdown": -0.4},
            "annual": annual,
        }
    }

    assert qualifies_oos(result)
    result["oos_stress"]["annual"]["2026"]["net_return"] = -0.01
    assert not qualifies_oos(result)
