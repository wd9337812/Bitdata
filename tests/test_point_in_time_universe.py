from __future__ import annotations

import io
import json
import zipfile

import pandas as pd

from scripts.benchmark_s0_xmom_point_in_time import (
    load_manifest,
    selected_signals,
    symbol_start_ms,
)
from scripts.build_binance_um_point_in_time_1h import (
    KLINE_COLUMNS,
    is_crypto_perpetual,
    month_range,
    parse_kline_archive,
    parse_object_keys,
    parse_symbol_prefixes,
)
from scripts.extend_binance_um_point_in_time_daily_1h import (
    date_from_key,
    date_range,
    merge_daily_frame,
)


def test_month_range_is_inclusive() -> None:
    assert month_range("2026-01", "2026-03") == [
        "2026-01",
        "2026-02",
        "2026-03",
    ]


def test_date_range_is_inclusive() -> None:
    assert date_range("2026-07-01", "2026-07-03") == [
        "2026-07-01",
        "2026-07-02",
        "2026-07-03",
    ]


def test_daily_key_date_parser() -> None:
    key = (
        "data/futures/um/daily/klines/AUSDT/1h/"
        "AUSDT-1h-2026-07-30.zip"
    )
    assert date_from_key(key) == "2026-07-30"


def test_s3_xml_parsers() -> None:
    payload = b"""<?xml version="1.0" encoding="UTF-8"?>
    <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
      <CommonPrefixes><Prefix>data/futures/um/monthly/klines/AUSDT/</Prefix></CommonPrefixes>
      <Contents><Key>data/futures/um/monthly/klines/AUSDT/1h/AUSDT-1h-2026-01.zip</Key></Contents>
    </ListBucketResult>"""
    assert parse_symbol_prefixes(payload) == ["AUSDT"]
    assert parse_object_keys(payload) == [
        "data/futures/um/monthly/klines/AUSDT/1h/AUSDT-1h-2026-01.zip"
    ]


def test_crypto_perpetual_filter_excludes_tradfi() -> None:
    assert is_crypto_perpetual(
        "ETHUSDT",
        {"contractType": "PERPETUAL", "underlyingType": "COIN"},
    )
    assert not is_crypto_perpetual(
        "AAPLUSDT",
        {"contractType": "TRADIFI_PERPETUAL", "underlyingType": "EQUITY"},
    )
    assert is_crypto_perpetual("DELISTEDUSDT", None)
    assert not is_crypto_perpetual("BTCUSDT_260925", None)


def test_archive_parser_assigns_types_and_symbol() -> None:
    row = [
        1_700_000_000_000,
        1.0,
        2.0,
        0.5,
        1.5,
        100.0,
        1_700_003_599_999,
        150.0,
        10,
        60.0,
        90.0,
        0,
    ]
    csv = ",".join(KLINE_COLUMNS) + "\n" + ",".join(map(str, row)) + "\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("AUSDT-1h-2026-01.csv", csv)
    result = parse_kline_archive(buffer.getvalue(), "AUSDT")
    assert result.loc[0, "symbol"] == "AUSDT"
    assert result.loc[0, "open_time"] == row[0]
    assert result.loc[0, "close"] == 1.5


def test_daily_merge_replaces_duplicate_hour() -> None:
    existing = pd.DataFrame(
        {"open_time": [1, 2], "close": [10.0, 20.0]}
    )
    daily = [pd.DataFrame({"open_time": [2, 3], "close": [21.0, 30.0]})]
    result = merge_daily_frame(existing, daily)
    assert result.open_time.tolist() == [1, 2, 3]
    assert result.close.tolist() == [10.0, 21.0, 30.0]


def test_manifest_merges_new_daily_symbols(tmp_path) -> None:
    monthly = {
        "symbols": [
            {
                "symbol": "OLDUSDT",
                "first_archive_month": "2025-01",
                "archive_months": ["2026-01"],
                "first_open_time": 1_735_689_600_000,
            }
        ],
        "failures": [],
    }
    daily = {
        "source": "daily",
        "start_date": "2026-07-01",
        "end_date": "2026-07-30",
        "checksum_verified": True,
        "symbols": [
            {
                "symbol": "NEWUSDT",
                "first_archive_month": "2026-07",
                "archive_months": ["2026-07"],
                "archive_dates": ["2026-07-10"],
                "first_open_time": 1_783_641_600_000,
            }
        ],
        "failures": [],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(monthly))
    (tmp_path / "daily_extension_manifest.json").write_text(json.dumps(daily))
    starts, payload = load_manifest(tmp_path)
    assert set(starts) == {"OLDUSDT", "NEWUSDT"}
    assert payload["daily_extension"]["end_date"] == "2026-07-30"


def test_symbol_start_uses_archive_month_before_download_window() -> None:
    record = {
        "first_archive_month": "2020-09",
        "archive_months": ["2026-01", "2026-02"],
        "first_open_time": 1_767_225_600_000,
    }
    expected = int(pd.Timestamp("2020-09-01").timestamp() * 1000)
    assert symbol_start_ms(record) == expected


def test_symbol_start_uses_first_bar_for_new_listing() -> None:
    record = {
        "first_archive_month": "2026-01",
        "archive_months": ["2026-01", "2026-02"],
        "first_open_time": 1_768_435_200_000,
    }
    assert symbol_start_ms(record) == 1_768_435_200_000


def test_live_equivalent_selection_applies_absolute_volume_before_breadth() -> None:
    rows = []
    timestamp = 1_800_000_000_000
    rows.append(
        {
            "available_ms": timestamp,
            "symbol": "BTCUSDT",
            "ret_24h": 0.02,
            "liquidity_24h": 1_000_000_000.0,
            "symbol_age_days": 100.0,
        }
    )
    rows.append(
        {
            "available_ms": timestamp,
            "symbol": "ETHUSDT",
            "ret_24h": -0.50,
            "liquidity_24h": 1_000_000_000.0,
            "symbol_age_days": 100.0,
        }
    )
    for index in range(60):
        rows.append(
            {
                "available_ms": timestamp,
                "symbol": f"ALT{index}USDT",
                "ret_24h": 0.01 + index / 10_000,
                "liquidity_24h": 6_000_000.0,
                "symbol_age_days": 100.0,
            }
        )
    rows.append(
        {
            "available_ms": timestamp,
            "symbol": "ILLIQUIDUSDT",
            "ret_24h": 10.0,
            "liquidity_24h": 1_000.0,
            "symbol_age_days": 100.0,
        }
    )
    selected, stats = selected_signals(pd.DataFrame(rows), 30)
    assert selected.iloc[0].symbol == "ALT59USDT"
    assert stats["minimum_24h_quote_volume"] == 5_000_000.0
    assert stats["selected_after_age_gate"] == 1
