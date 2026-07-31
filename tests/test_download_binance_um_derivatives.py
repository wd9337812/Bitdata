from __future__ import annotations

import io
import zipfile

import pandas as pd

from scripts.download_binance_um_derivatives import (
    _combine_funding,
    _combine_metrics,
    build_tasks,
    parse_archive,
)


def _archive(name: str, frame: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, frame.to_csv(index=False))
    return buffer.getvalue()


def test_build_tasks_includes_prior_period_for_backward_features() -> None:
    tasks = build_tasks(
        {
            "BTCUSDT": (
                int(pd.Timestamp("2026-02-03T12:00:00Z").timestamp() * 1000),
                int(pd.Timestamp("2026-02-04T12:00:00Z").timestamp() * 1000),
            )
        }
    )
    metric_periods = [task.period for task in tasks if task.kind == "metrics"]
    funding_periods = [task.period for task in tasks if task.kind == "funding"]
    assert metric_periods == ["2026-02-02", "2026-02-03", "2026-02-04"]
    assert funding_periods == ["2026-01", "2026-02"]


def test_parse_and_combine_metric_archive() -> None:
    frame = pd.DataFrame(
        [
            {
                "create_time": "2026-07-29 00:00:00",
                "symbol": "BTCUSDT",
                "sum_open_interest": 10,
                "sum_open_interest_value": 100,
                "count_toptrader_long_short_ratio": 1.1,
                "sum_toptrader_long_short_ratio": 1.2,
                "count_long_short_ratio": 1.3,
                "sum_taker_long_short_vol_ratio": 1.4,
            }
        ]
    )
    parsed = parse_archive(_archive("metrics.csv", frame), "metrics")
    combined = _combine_metrics([parsed], "BTCUSDT")
    assert combined.loc[0, "timestamp_ms"] == 1785283200000
    assert combined.loc[0, "sum_taker_long_short_vol_ratio"] == 1.4


def test_parse_and_combine_funding_archive() -> None:
    frame = pd.DataFrame(
        [{"calc_time": 1785283200000, "funding_interval_hours": 8, "last_funding_rate": 0.0001}]
    )
    parsed = parse_archive(_archive("funding.csv", frame), "funding")
    combined = _combine_funding([parsed], "BTCUSDT")
    assert combined.loc[0, "timestamp_ms"] == 1785283200000
    assert combined.loc[0, "last_funding_rate"] == 0.0001
