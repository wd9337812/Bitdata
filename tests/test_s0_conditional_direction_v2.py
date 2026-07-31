from __future__ import annotations

from scripts import train_s0_conditional_direction_v1 as base
from scripts import train_s0_conditional_direction_v2 as v2


def test_v2_configures_isolated_longer_horizon_research():
    original_version = base.MODEL_VERSION
    original_horizons = base.HORIZONS
    original_output = base.DEFAULT_OUTPUT
    try:
        v2.configure()
        assert base.MODEL_VERSION == "s0_conditional_direction_v2"
        assert list(base.HORIZONS) == ["10m", "30m", "60m"]
        assert base.DEFAULT_OUTPUT.name == "s0_conditional_direction_v2"
    finally:
        base.MODEL_VERSION = original_version
        base.HORIZONS = original_horizons
        base.DEFAULT_OUTPUT = original_output

