from __future__ import annotations

import pandas as pd

from scripts.download_binance_um_aggtrades import (
    _aggregate_chunk,
    archive_tasks,
    combine_partials,
    finalize_features,
)


def test_archive_tasks_use_monthly_only_for_complete_months() -> None:
    start = int(pd.Timestamp("2026-01-10T00:00:00Z").timestamp() * 1000)
    end = int(pd.Timestamp("2026-03-15T00:00:00Z").timestamp() * 1000)
    tasks = archive_tasks("BTCUSDT", start, end)
    periods = [task.period for task in tasks]
    assert "2026-02" in periods
    assert "2026-01" not in periods
    assert "2026-03" not in periods
    assert periods[0] == "2026-01-10"
    assert periods[-1] == "2026-03-15"


def test_aggregate_trade_direction_uses_taker_side() -> None:
    raw = pd.DataFrame(
        [
            [1, 100.0, 2.0, 1, 1, 60_010, False],
            [2, 100.0, 1.0, 2, 2, 60_020, True],
        ],
        columns=[
            "agg_trade_id",
            "price",
            "quantity",
            "first_trade_id",
            "last_trade_id",
            "transact_time",
            "is_buyer_maker",
        ],
    )
    partial = _aggregate_chunk(raw)
    result = finalize_features(combine_partials([partial]))
    assert result.loc[0, "available_ms"] == 120_000
    assert result.loc[0, "agg_trade_count_1m"] == 2
    assert round(result.loc[0, "agg_taker_imbalance_1m"], 6) == round(1 / 3, 6)
    assert round(result.loc[0, "agg_signed_count_ratio_1m"], 6) == 0.0
    assert round(result.loc[0, "agg_max_share_1m"], 6) == round(2 / 3, 6)


def test_chunk_partials_preserve_first_last_and_sums() -> None:
    columns = [
        "agg_trade_id",
        "price",
        "quantity",
        "first_trade_id",
        "last_trade_id",
        "transact_time",
        "is_buyer_maker",
    ]
    first = _aggregate_chunk(
        pd.DataFrame([[1, 100.0, 1.0, 1, 1, 60_010, False]], columns=columns)
    )
    second = _aggregate_chunk(
        pd.DataFrame([[2, 102.0, 1.0, 2, 2, 60_020, False]], columns=columns)
    )
    combined = combine_partials([first, second])
    assert combined.loc[0, "trade_count"] == 2
    assert combined.loc[0, "price_first"] == 100.0
    assert combined.loc[0, "price_last"] == 102.0
    assert combined.loc[0, "price_high"] == 102.0
