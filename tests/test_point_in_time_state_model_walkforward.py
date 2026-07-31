from __future__ import annotations

from scripts.benchmark_s0_point_in_time_state_model_walkforward import (
    WALK_FORWARD_CYCLES,
)


def test_walk_forward_cycles_are_strictly_ordered() -> None:
    for cycle in WALK_FORWARD_CYCLES:
        assert cycle["train"][1] == cycle["calibration"][0]
        assert cycle["calibration"][1] == cycle["evaluate"][0]
