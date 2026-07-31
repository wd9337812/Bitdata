import numpy as np
import pandas as pd
import pytest

from scripts.benchmark_s0_tail_event_model import (
    BASE_COST,
    choose_threshold,
    metrics,
    path_return,
)


def test_path_return_uses_stop_when_same_bar_hits_both():
    entry = np.array([100.0])
    value = path_return(entry, [np.array([102.0])], [np.array([99.0])], [np.array([101.0])], 1)
    assert value[0] == -0.006 - BASE_COST


def test_path_return_can_take_short_profit():
    entry = np.array([100.0])
    value = path_return(entry, [np.array([100.2])], [np.array([98.5])], [np.array([99.0])], -1)
    assert value[0] == 0.01 - BASE_COST


def test_choose_threshold_requires_cost_positive_validation():
    reports = [
        {"threshold": 0.4, "base": {"trades": 40, "profit_factor": 1.2}, "stress": {"profit_factor": 1.1, "net_return": 0.2}},
        {"threshold": 0.5, "base": {"trades": 40, "profit_factor": 1.3}, "stress": {"profit_factor": 1.2, "net_return": -0.1}},
    ]
    assert choose_threshold(reports) == 0.4


def test_metrics_applies_extra_stress_cost():
    trades = pd.DataFrame({"net_return": [0.01, -0.005]})
    assert metrics(trades)["net_return"] == 0.005
    assert metrics(trades, 0.001)["net_return"] == pytest.approx(0.003)
