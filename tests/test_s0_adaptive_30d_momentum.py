import pandas as pd
import pytest

from scripts.benchmark_s0_adaptive_30d_momentum import (
    apply_direction_gate,
    profit_factor,
    qualifies,
)
from scripts.validate_s0_adaptive_30d_momentum_minute import minute_scenario_passes


def _trade(index: int, direction: str, net: float) -> dict:
    return {
        "entry_ms": index * 10,
        "exit_ms": index * 10 + 5,
        "direction": direction,
        "net_pct": net,
    }


def test_profit_factor_handles_empty_loss_denominator():
    assert profit_factor([1.0, 2.0]) == 999
    assert profit_factor([]) == 0
    assert profit_factor([2.0, -1.0]) == 2


def test_direction_gate_uses_prior_paper_outcomes_and_can_recover():
    frame = pd.DataFrame(
        [
            _trade(0, "LONG", -1),
            _trade(1, "LONG", -1),
            _trade(2, "LONG", -1),
            _trade(3, "LONG", 5),
            _trade(4, "LONG", 5),
        ]
    )
    selected = apply_direction_gate(frame)
    assert list(selected.entry_ms) == [0, 10, 20, 40]


def test_direction_gate_rejects_overlapping_outcome_history():
    frame = pd.DataFrame(
        [
            {**_trade(0, "LONG", 1), "exit_ms": 20},
            _trade(1, "LONG", 1),
        ]
    )
    with pytest.raises(ValueError):
        apply_direction_gate(frame)


def test_direction_gate_is_independent_of_dataframe_index_labels():
    frame = pd.DataFrame(
        [
            _trade(0, "SHORT", 1),
            _trade(1, "SHORT", 1),
            _trade(2, "SHORT", 1),
        ],
        index=[10, 20, 30],
    )
    selected = apply_direction_gate(frame)
    assert list(selected.entry_ms) == [0, 10, 20]


def _metrics(trades=20, pf=1.5, net=1.0):
    return {"trades": trades, "profit_factor": pf, "net_pct_points": net}


def _report():
    return {
        "overall": _metrics(80),
        "annual": {str(year): _metrics() for year in range(2021, 2027)},
        "cohorts": {"blind": _metrics(20)},
        "without_top_3_symbols": _metrics(60),
        "bootstrap": {"positive_probability": 0.99},
    }


def test_qualification_requires_every_year_and_concentration_gates():
    base, stress = _report(), _report()
    assert qualifies(base, stress)
    stress["annual"]["2023"] = _metrics(net=-1)
    assert not qualifies(base, stress)
    stress = _report()
    stress["without_top_3_symbols"] = _metrics(net=-1)
    assert not qualifies(base, stress)


def _minute_scenario():
    return {
        "funded": {
            "overall": {**_metrics(30), "symbols": 10},
            "cohorts": {"blind": _metrics(10)},
            "without_top_3_symbols": _metrics(20),
            "bootstrap": {"positive_probability": 0.97},
        }
    }


def test_minute_qualification_rejects_winner_concentration_and_weak_blind_cohort():
    scenario = _minute_scenario()
    assert minute_scenario_passes(scenario)

    scenario = _minute_scenario()
    scenario["funded"]["without_top_3_symbols"] = _metrics(net=-1)
    assert not minute_scenario_passes(scenario)

    scenario = _minute_scenario()
    scenario["funded"]["cohorts"]["blind"] = _metrics(4, pf=0.8, net=-1)
    assert not minute_scenario_passes(scenario)

    scenario = _minute_scenario()
    scenario["funded"]["overall"]["symbols"] = 5
    assert not minute_scenario_passes(scenario)
