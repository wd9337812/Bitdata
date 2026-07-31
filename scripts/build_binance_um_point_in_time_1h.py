from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = (
    ROOT / "data" / "research" / "binance_um_point_in_time_1h"
)
S3_BUCKET = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
ARCHIVE_BASE = "https://data.binance.vision"
EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
S3_NAMESPACE = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
ARCHIVE_PATTERN = re.compile(
    r"data/futures/um/monthly/klines/"
    r"(?P<symbol>[^/]+)/1h/(?P=symbol)-1h-(?P<month>\d{4}-\d{2})\.zip$"
)
KLINE_COLUMNS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "count",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "ignore",
]
NUMERIC_COLUMNS = [
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "taker_buy_volume",
    "taker_buy_quote_volume",
]


@dataclass(frozen=True)
class SymbolArchives:
    symbol: str
    first_archive_month: str
    archives: tuple[str, ...]
    current_metadata: dict[str, Any] | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a point-in-time Binance USD-M crypto perpetual 1h dataset "
            "from the official public archive, including delisted contracts."
        )
    )
    parser.add_argument("--start-month", default="2026-01")
    parser.add_argument("--end-month", default="2026-06")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--no-checksum", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def month_range(start: str, end: str) -> list[str]:
    start_period = pd.Period(start, freq="M")
    end_period = pd.Period(end, freq="M")
    if end_period < start_period:
        raise ValueError("end-month must not be earlier than start-month")
    return [
        str(item)
        for item in pd.period_range(start_period, end_period, freq="M")
    ]


def parse_symbol_prefixes(payload: bytes) -> list[str]:
    root = ET.fromstring(payload)
    symbols: list[str] = []
    for item in root.findall("s3:CommonPrefixes/s3:Prefix", S3_NAMESPACE):
        prefix = str(item.text or "").rstrip("/")
        if prefix:
            symbols.append(prefix.rsplit("/", 1)[-1])
    return symbols


def parse_object_keys(payload: bytes) -> list[str]:
    root = ET.fromstring(payload)
    return [
        str(item.text)
        for item in root.findall("s3:Contents/s3:Key", S3_NAMESPACE)
        if item.text
    ]


def current_symbol_metadata(
    session: requests.Session,
    timeout: int = 30,
) -> dict[str, dict[str, Any]]:
    response = session.get(EXCHANGE_INFO_URL, timeout=timeout)
    response.raise_for_status()
    return {
        str(item.get("symbol")): item
        for item in response.json().get("symbols", [])
        if item.get("symbol")
    }


def is_crypto_perpetual(
    symbol: str,
    metadata: dict[str, Any] | None,
) -> bool:
    if not symbol.endswith("USDT") or "_" in symbol:
        return False
    if metadata is None:
        # Symbols absent from current exchangeInfo may be delisted. USD-M
        # historical USDT symbols pre-dating TradFi are retained so that the
        # backtest does not silently discard failed contracts.
        return True
    return (
        str(metadata.get("contractType") or "") == "PERPETUAL"
        and str(metadata.get("underlyingType") or "") == "COIN"
    )


def request_with_retry(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    retries: int = 4,
    timeout: int = 45,
) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = session.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(min(2**attempt, 8))
    assert last_error is not None
    raise last_error


def list_archives_for_symbol(
    symbol: str,
    metadata: dict[str, Any] | None,
    selected_months: set[str],
    retries: int,
) -> SymbolArchives | None:
    session = requests.Session()
    prefix = f"data/futures/um/monthly/klines/{symbol}/1h/"
    response = request_with_retry(
        session,
        S3_BUCKET,
        params={"list-type": "2", "prefix": prefix},
        retries=retries,
    )
    matches = [
        (match.group("month"), key)
        for key in parse_object_keys(response.content)
        if (match := ARCHIVE_PATTERN.fullmatch(key))
    ]
    if not matches:
        return None
    matches.sort()
    selected = tuple(
        key for month, key in matches if month in selected_months
    )
    if not selected:
        return None
    return SymbolArchives(
        symbol=symbol,
        first_archive_month=matches[0][0],
        archives=selected,
        current_metadata=metadata,
    )


def discover_archives(
    months: list[str],
    workers: int,
    retries: int,
) -> list[SymbolArchives]:
    session = requests.Session()
    metadata = current_symbol_metadata(session)
    response = request_with_retry(
        session,
        S3_BUCKET,
        params={
            "list-type": "2",
            "prefix": "data/futures/um/monthly/klines/",
            "delimiter": "/",
        },
        retries=retries,
    )
    symbols = [
        symbol
        for symbol in parse_symbol_prefixes(response.content)
        if is_crypto_perpetual(symbol, metadata.get(symbol))
    ]
    discovered: list[SymbolArchives] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                list_archives_for_symbol,
                symbol,
                metadata.get(symbol),
                set(months),
                retries,
            ): symbol
            for symbol in symbols
        }
        for future in as_completed(futures):
            item = future.result()
            if item is not None:
                discovered.append(item)
    return sorted(discovered, key=lambda item: item.symbol)


def verify_checksum(
    session: requests.Session,
    archive_url: str,
    payload: bytes,
    retries: int,
) -> None:
    response = request_with_retry(
        session,
        archive_url + ".CHECKSUM",
        retries=retries,
    )
    expected = response.text.strip().split()[0].lower()
    actual = hashlib.sha256(payload).hexdigest()
    if not expected or actual != expected:
        raise ValueError(
            f"Checksum mismatch for {archive_url}: {actual} != {expected}"
        )


def parse_kline_archive(payload: bytes, symbol: str) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = [
            name for name in archive.namelist() if name.lower().endswith(".csv")
        ]
        if len(names) != 1:
            raise ValueError(
                f"{symbol}: expected one CSV in archive, found {len(names)}"
            )
        with archive.open(names[0]) as handle:
            frame = pd.read_csv(handle)
    if list(frame.columns) != KLINE_COLUMNS:
        if frame.shape[1] != len(KLINE_COLUMNS):
            raise ValueError(
                f"{symbol}: unexpected kline columns {list(frame.columns)}"
            )
        frame.columns = KLINE_COLUMNS
    frame["symbol"] = symbol
    frame["open_time"] = pd.to_numeric(frame.open_time, errors="raise").astype(
        "int64"
    )
    frame["close_time"] = pd.to_numeric(frame.close_time, errors="raise").astype(
        "int64"
    )
    frame["count"] = pd.to_numeric(frame["count"], errors="coerce").fillna(0).astype(
        "int64"
    )
    for column in NUMERIC_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="raise").astype(
            "float64"
        )
    return frame[
        [
            "symbol",
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "quote_volume",
            "count",
            "taker_buy_volume",
            "taker_buy_quote_volume",
        ]
    ]


def month_from_key(key: str) -> str:
    match = ARCHIVE_PATTERN.fullmatch(key)
    if not match:
        raise ValueError(f"Unexpected archive key: {key}")
    return match.group("month")


def build_symbol(
    item: SymbolArchives,
    output: Path,
    retries: int,
    verify: bool,
    force: bool,
) -> dict[str, Any]:
    target = output / "parquet" / f"{item.symbol}.parquet"
    if target.exists() and not force:
        existing = pd.read_parquet(target, columns=["open_time"])
        return {
            "symbol": item.symbol,
            "first_archive_month": item.first_archive_month,
            "archive_months": [month_from_key(key) for key in item.archives],
            "rows": int(len(existing)),
            "first_open_time": int(existing.open_time.min()),
            "last_open_time": int(existing.open_time.max()),
            "cached": True,
        }
    session = requests.Session()
    parts: list[pd.DataFrame] = []
    for key in item.archives:
        url = f"{ARCHIVE_BASE}/{key}"
        response = request_with_retry(session, url, retries=retries)
        if verify:
            verify_checksum(session, url, response.content, retries)
        parts.append(parse_kline_archive(response.content, item.symbol))
    frame = (
        pd.concat(parts, ignore_index=True)
        .drop_duplicates("open_time", keep="last")
        .sort_values("open_time")
        .reset_index(drop=True)
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp.parquet")
    frame.to_parquet(temporary, index=False, compression="zstd")
    temporary.replace(target)
    metadata = item.current_metadata or {}
    return {
        "symbol": item.symbol,
        "first_archive_month": item.first_archive_month,
        "archive_months": [month_from_key(key) for key in item.archives],
        "rows": int(len(frame)),
        "first_open_time": int(frame.open_time.min()),
        "last_open_time": int(frame.open_time.max()),
        "cached": False,
        "current_status": metadata.get("status"),
        "current_underlying_type": metadata.get("underlyingType"),
        "current_contract_type": metadata.get("contractType"),
    }


def main() -> None:
    args = parse_args()
    months = month_range(args.start_month, args.end_month)
    args.output.mkdir(parents=True, exist_ok=True)
    discovered = discover_archives(months, args.workers, args.retries)
    print(
        json.dumps(
            {
                "status": "discovered",
                "symbols": len(discovered),
                "archives": sum(len(item.archives) for item in discovered),
                "months": months,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    records: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                build_symbol,
                item,
                args.output,
                args.retries,
                not args.no_checksum,
                args.force,
            ): item.symbol
            for item in discovered
        }
        total = len(futures)
        for completed, future in enumerate(as_completed(futures), 1):
            symbol = futures[future]
            try:
                records.append(future.result())
            except Exception as exc:
                failures.append({"symbol": symbol, "error": str(exc)})
            if completed % 25 == 0 or completed == total:
                print(
                    json.dumps(
                        {
                            "status": "downloading",
                            "completed": completed,
                            "total": total,
                            "failures": len(failures),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
    manifest = {
        "source": "Binance official data.binance.vision USD-M monthly 1h klines",
        "s3_bucket": S3_BUCKET,
        "start_month": args.start_month,
        "end_month": args.end_month,
        "checksum_verified": not args.no_checksum,
        "symbols": sorted(records, key=lambda item: item["symbol"]),
        "failures": sorted(failures, key=lambda item: item["symbol"]),
    }
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": "complete",
                "symbols": len(records),
                "rows": sum(int(item["rows"]) for item in records),
                "failures": len(failures),
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
