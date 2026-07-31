from __future__ import annotations

import pandas as pd
import pytest

from scripts.train_s0_orderbook_v1 import (
    BOOK_NUMERIC,
    enrich_orderbook,
    merge_symbol_book,
)


def test_book_join_never_uses_an_unfinished_future_bucket() -> None:
    candidates = pd.DataFrame(
        [
            {
                "open_time": 599_999,
                "symbol": "BTCUSDT",
                "direction": "LONG",
            },
            {
                "open_time": 600_000,
                "symbol": "BTCUSDT",
                "direction": "LONG",
            },
        ]
    )
    book = pd.DataFrame(
        [
            {"available_ms": 300_000, "book_snapshot_count": 10},
            {"available_ms": 600_000, "book_snapshot_count": 10},
        ]
    )
    merged = merge_symbol_book(candidates, book)
    assert merged.available_ms.tolist() == [300_000, 600_000]
    assert merged.book_age_minutes.tolist() == pytest.approx(
        [4.999983333333333, 0.0]
    )


def test_orderbook_features_are_directional_and_require_fresh_depth(
    tmp_path,
) -> None:
    candidates = pd.DataFrame(
        [
            {
                "open_time": 600_000,
                "symbol": "BTCUSDT",
                "direction": "LONG",
            },
            {
                "open_time": 600_000,
                "symbol": "BTCUSDT",
                "direction": "SHORT",
            },
            {
                "open_time": 1_300_001,
                "symbol": "BTCUSDT",
                "direction": "LONG",
            },
        ]
    )
    row = {
        "available_ms": 600_000,
        "symbol": "BTCUSDT",
        "book_snapshot_count": 10,
        "book_imbalance_0.2_mean": 0.4,
        "book_imbalance_0.2_last": 0.5,
        "book_imbalance_0.2_std": 0.1,
        "book_imbalance_0.2_persistence": 0.8,
        "book_depth_log_0.2_mean": 10.0,
        "book_imbalance_1_mean": 0.3,
        "book_imbalance_1_last": 0.2,
        "book_imbalance_1_std": 0.1,
        "book_imbalance_1_persistence": 0.7,
        "book_depth_log_1_mean": 11.0,
        "book_imbalance_5_mean": 0.1,
        "book_imbalance_5_last": 0.1,
        "book_imbalance_5_std": 0.05,
        "book_imbalance_5_persistence": 0.6,
        "book_depth_log_5_mean": 12.0,
        "book_near_share_mean": 0.2,
        "book_imbalance_0.2_slope": 0.1,
    }
    pd.DataFrame([row]).to_parquet(
        tmp_path / "BTCUSDT-book-depth.parquet",
        index=False,
    )
    result = enrich_orderbook(candidates, tmp_path)
    assert len(result) == 2
    assert result.iloc[0]["book_imbalance_0.2_mean_dir"] == 0.4
    assert result.iloc[1]["book_imbalance_0.2_mean_dir"] == -0.4
    assert not result[list(BOOK_NUMERIC)].isna().any().any()
