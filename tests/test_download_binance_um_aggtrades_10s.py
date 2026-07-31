from __future__ import annotations

import pandas as pd

from scripts.download_binance_um_aggtrades_10s import (
    aggregate_chunk_10s,
    combine_partials_10s,
    daily_tasks,
    finalize_10s,
)


COLUMNS = [
    "agg_trade_id",
    "price",
    "quantity",
    "first_trade_id",
    "last_trade_id",
    "transact_time",
    "is_buyer_maker",
]


def test_daily_tasks_are_inclusive_and_symbol_major() -> None:
    tasks = daily_tasks(["btcusdt", "ethusdt"], "2026-01-01", "2026-01-02")
    assert [(task.symbol, task.date) for task in tasks] == [
        ("BTCUSDT", "2026-01-01"),
        ("BTCUSDT", "2026-01-02"),
        ("ETHUSDT", "2026-01-01"),
        ("ETHUSDT", "2026-01-02"),
    ]


def test_exact_10s_aggregation_uses_taker_side_and_availability() -> None:
    raw = pd.DataFrame(
        [
            [1, 100.0, 2.0, 1, 1, 10_010, False],
            [2, 102.0, 1.0, 2, 2, 10_020, True],
            [3, 103.0, 1.0, 3, 3, 20_010, False],
        ],
        columns=COLUMNS,
    )
    result = finalize_10s(combine_partials_10s([aggregate_chunk_10s(raw)]))
    assert list(result.bin_ms) == [10_000, 20_000]
    assert result.loc[0, "available_ms"] == 20_000
    assert result.loc[0, "open"] == 100.0
    assert result.loc[0, "close"] == 102.0
    assert round(result.loc[0, "order_imbalance"], 6) == round(1 / 3, 6)


def test_chunk_boundary_preserves_open_and_close() -> None:
    first = aggregate_chunk_10s(
        pd.DataFrame([[1, 100.0, 1.0, 1, 1, 10_010, False]], columns=COLUMNS)
    )
    second = aggregate_chunk_10s(
        pd.DataFrame([[2, 102.0, 1.0, 2, 2, 10_020, False]], columns=COLUMNS)
    )
    result = combine_partials_10s([first, second])
    assert result.loc[0, "trade_count"] == 2
    assert result.loc[0, "open"] == 100.0
    assert result.loc[0, "close"] == 102.0
