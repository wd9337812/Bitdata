from __future__ import annotations

import pandas as pd

from scripts.download_binance_um_book_depth import _existing_coverage, aggregate_depth


def test_depth_aggregation_uses_bid_minus_ask_and_closes_at_bucket_end() -> None:
    frame = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-07-29T00:00:30Z"),
                "percentage": -0.2,
                "notional": 300,
            },
            {
                "timestamp": pd.Timestamp("2026-07-29T00:00:30Z"),
                "percentage": 0.2,
                "notional": 100,
            },
            {
                "timestamp": pd.Timestamp("2026-07-29T00:00:30Z"),
                "percentage": -1.0,
                "notional": 600,
            },
            {
                "timestamp": pd.Timestamp("2026-07-29T00:00:30Z"),
                "percentage": 1.0,
                "notional": 400,
            },
            {
                "timestamp": pd.Timestamp("2026-07-29T00:00:30Z"),
                "percentage": -5.0,
                "notional": 1000,
            },
            {
                "timestamp": pd.Timestamp("2026-07-29T00:00:30Z"),
                "percentage": 5.0,
                "notional": 1000,
            },
        ]
    )
    result = aggregate_depth(frame)
    assert result.loc[0, "available_ms"] == 1785283500000
    assert result.loc[0, "book_imbalance_0.2_mean"] == 0.5
    assert result.loc[0, "book_near_band_available"] == 1.0


def test_depth_aggregation_keeps_wider_bands_when_near_band_is_missing() -> None:
    frame = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-01-01T00:00:30Z"),
                "percentage": -1.0,
                "notional": 600,
            },
            {
                "timestamp": pd.Timestamp("2026-01-01T00:00:30Z"),
                "percentage": 1.0,
                "notional": 400,
            },
            {
                "timestamp": pd.Timestamp("2026-01-01T00:00:30Z"),
                "percentage": -5.0,
                "notional": 1000,
            },
            {
                "timestamp": pd.Timestamp("2026-01-01T00:00:30Z"),
                "percentage": 5.0,
                "notional": 1000,
            },
        ]
    )
    result = aggregate_depth(frame)
    assert len(result) == 1
    assert result.loc[0, "book_imbalance_1_mean"] == 0.2
    assert pd.isna(result.loc[0, "book_imbalance_0.2_mean"])
    assert result.loc[0, "book_near_band_available"] == 0.0
    assert result.loc[0, "book_imbalance_1_mean"] == 0.2
    assert result.loc[0, "book_imbalance_5_mean"] == 0.0


def test_depth_aggregation_tracks_persistence_and_last_value() -> None:
    rows = []
    for timestamp, bid, ask in (
        ("2026-07-29T00:00:30Z", 300, 100),
        ("2026-07-29T00:01:00Z", 100, 300),
        ("2026-07-29T00:01:30Z", 400, 100),
    ):
        for band in (0.2, 1.0, 5.0):
            rows.extend(
                [
                    {"timestamp": pd.Timestamp(timestamp), "percentage": -band, "notional": bid},
                    {"timestamp": pd.Timestamp(timestamp), "percentage": band, "notional": ask},
                ]
            )
    result = aggregate_depth(pd.DataFrame(rows))
    assert round(result.loc[0, "book_imbalance_0.2_last"], 6) == 0.6
    assert round(result.loc[0, "book_imbalance_0.2_persistence"], 6) == round(1 / 3, 6)
    assert round(result.loc[0, "book_imbalance_0.2_slope"], 6) == 0.1


def test_existing_nonempty_feature_file_is_resumable(tmp_path) -> None:
    path = tmp_path / "BTCUSDT-book-depth.parquet"
    pd.DataFrame({"available_ms": [100, 200]}).to_parquet(path, index=False)
    result = _existing_coverage(path, "BTCUSDT", 10)
    assert result is not None
    assert result["cached"] is True
    assert result["rows"] == 2
