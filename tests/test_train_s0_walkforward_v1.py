from __future__ import annotations

import pandas as pd

from scripts.train_s0_walkforward_v1 import fold_boundaries


def test_walkforward_windows_use_only_trailing_data() -> None:
    frame = pd.DataFrame(
        {
            "time": pd.date_range(
                "2026-01-01",
                "2026-07-31",
                freq="5min",
                tz="UTC",
            )
        }
    )
    windows = fold_boundaries(frame)
    assert len(windows) == 4
    for window in windows:
        assert window.train_start < window.early_stop_start
        assert window.early_stop_start < window.calibration_start
        assert window.calibration_start < window.evaluation_start
        assert window.train_start == window.evaluation_start - pd.Timedelta(days=77)
        assert window.calibration_start == window.evaluation_start - pd.Timedelta(days=7)
    assert windows[-1].name == "untouched_test"
    assert windows[-1].evaluation_end is None
