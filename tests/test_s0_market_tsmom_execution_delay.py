import pandas as pd

from scripts.benchmark_s0_market_tsmom_execution_delay import (
    delay_signals,
    qualifies_scenarios,
)


def test_delay_moves_entry_and_exit_equally() -> None:
    signals = pd.DataFrame(
        {
            "entry_day": pd.to_datetime(["2024-01-01"], utc=True),
            "exit_day": pd.to_datetime(["2024-01-06"], utc=True),
        }
    )

    delayed = delay_signals(signals, 4)

    assert delayed.iloc[0].entry_day.hour == 4
    assert delayed.iloc[0].exit_day.hour == 4


def test_all_delay_scenarios_must_pass() -> None:
    annual = {
        year: {"net_return": 0.1, "profit_factor": 1.2}
        for year in ("2024", "2025", "2026")
    }
    passing = {
        "oos_stress": {
            "overall": {"profit_factor": 1.2, "max_drawdown": -0.4},
            "annual": annual,
        }
    }
    scenarios = {"delay_0h": passing, "delay_1h": passing}

    assert qualifies_scenarios(scenarios)
    scenarios["delay_1h"]["oos_stress"]["annual"]["2025"]["net_return"] = -0.1
    assert not qualifies_scenarios(scenarios)
