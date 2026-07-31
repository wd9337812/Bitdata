from __future__ import annotations

import pandas as pd
import pytest

from scripts.train_s0_quarter_phase_v1 import add_phase_features


@pytest.mark.parametrize(
    ("timestamp", "quarter", "hour", "funding"),
    [
        ("2026-07-29T08:00:00Z", 1, 1, 1),
        ("2026-07-29T08:15:00Z", 1, 0, 0),
        ("2026-07-29T08:05:00Z", 0, 0, 0),
    ],
)
def test_phase_boundaries(
    timestamp: str,
    quarter: int,
    hour: int,
    funding: int,
) -> None:
    frame = pd.DataFrame(
        [
            {
                "timestamp_ms": int(pd.Timestamp(timestamp).timestamp() * 1000),
                "direction": "SHORT",
                "deriv_taker_bias": 0.4,
                "deriv_taker_change_3": 0.2,
                "deriv_oi_change_3": 0.1,
                "ret_24_dir": 0.3,
                "volume_persistence": 0.2,
                "directed_flow": 0.1,
            }
        ]
    )
    result = add_phase_features(frame).iloc[0]
    assert result.phase_is_quarter_open == quarter
    assert result.phase_is_hour_open == hour
    assert result.phase_is_funding_window == funding
    assert result.phase_taker_dir == pytest.approx(-0.4 * quarter)
