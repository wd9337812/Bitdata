from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_binance_um_point_in_time_1h import (
    ARCHIVE_BASE,
    DEFAULT_OUTPUT,
    S3_BUCKET,
    current_symbol_metadata,
    is_crypto_perpetual,
    parse_kline_archive,
    parse_object_keys,
    parse_symbol_prefixes,
    request_with_retry,
    verify_checksum,
)


DAILY_PATTERN = re.compile(
    r"data/futures/um/daily/klines/"
    r"(?P<symbol>[^/]+)/1h/(?P=symbol)-1h-"
    r"(?P<date>\d{4}-\d{2}-\d{2})\.zip$"
)
EXTENSION_MANIFEST = "daily_extension_manifest.json"


@dataclass(frozen=True)
class DailySymbolArchives:
    symbol: str
    archives: tuple[str, ...]
    current_metadata: dict[str, Any] | None
    base_record: dict[str, Any] | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extend a Binance USD-M point-in-time dataset with checksum-"
            "verified official daily 1h archives."
        )
    )
    parser.add_argument("--start-date", default="2026-07-01")
    parser.add_argument("--end-date", default="2026-07-30")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workers", type=int, default=48)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--no-checksum", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def date_range(start: str, end: str) -> list[str]:
    start_day = pd.Timestamp(start).normalize()
    end_day = pd.Timestamp(end).normalize()
    if end_day < start_day:
        raise ValueError("end-date must not be earlier than start-date")
    return [
        str(item.date())
        for item in pd.date_range(start_day, end_day, freq="D")
    ]


def date_from_key(key: str) -> str:
    match = DAILY_PATTERN.fullmatch(key)
    if not match:
        raise ValueError(f"Unexpected daily archive key: {key}")
    return match.group("date")


def load_base_records(output: Path) -> dict[str, dict[str, Any]]:
    path = output / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(
            f"Monthly point-in-time manifest is required: {path}"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("failures"):
        raise ValueError("Monthly point-in-time manifest contains failures")
    return {
        str(item["symbol"]): item
        for item in payload.get("symbols", [])
    }


def load_cached_records(
    output: Path,
    selected_dates: set[str],
) -> dict[str, dict[str, Any]]:
    path = output / EXTENSION_MANIFEST
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("failures"):
        return {}
    records: dict[str, dict[str, Any]] = {}
    for item in payload.get("symbols", []):
        if selected_dates.issubset(set(item.get("archive_dates", []))):
            records[str(item["symbol"])] = item
    return records


def list_daily_archives_for_symbol(
    symbol: str,
    metadata: dict[str, Any] | None,
    base_record: dict[str, Any] | None,
    selected_dates: set[str],
    month_prefix: str,
    retries: int,
) -> DailySymbolArchives | None:
    session = requests.Session()
    prefix = (
        f"data/futures/um/daily/klines/{symbol}/1h/"
        f"{symbol}-1h-{month_prefix}"
    )
    response = request_with_retry(
        session,
        S3_BUCKET,
        params={"list-type": "2", "prefix": prefix},
        retries=retries,
    )
    selected: list[tuple[str, str]] = []
    for key in parse_object_keys(response.content):
        match = DAILY_PATTERN.fullmatch(key)
        if match and match.group("date") in selected_dates:
            selected.append((match.group("date"), key))
    if not selected:
        return None
    selected.sort()
    return DailySymbolArchives(
        symbol=symbol,
        archives=tuple(key for _, key in selected),
        current_metadata=metadata,
        base_record=base_record,
    )


def discover_daily_archives(
    output: Path,
    dates: list[str],
    workers: int,
    retries: int,
) -> list[DailySymbolArchives]:
    months = {date[:7] for date in dates}
    if len(months) != 1:
        raise ValueError(
            "One daily extension run must stay within one calendar month"
        )
    month_prefix = next(iter(months))
    selected_dates = set(dates)
    base_records = load_base_records(output)
    session = requests.Session()
    metadata = current_symbol_metadata(session)
    response = request_with_retry(
        session,
        S3_BUCKET,
        params={
            "list-type": "2",
            "prefix": "data/futures/um/daily/klines/",
            "delimiter": "/",
        },
        retries=retries,
    )
    symbols = [
        symbol
        for symbol in parse_symbol_prefixes(response.content)
        if is_crypto_perpetual(symbol, metadata.get(symbol))
    ]
    discovered: list[DailySymbolArchives] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                list_daily_archives_for_symbol,
                symbol,
                metadata.get(symbol),
                base_records.get(symbol),
                selected_dates,
                month_prefix,
                retries,
            ): symbol
            for symbol in symbols
        }
        for future in as_completed(futures):
            item = future.result()
            if item is not None:
                discovered.append(item)
    return sorted(discovered, key=lambda item: item.symbol)


def merge_daily_frame(
    existing: pd.DataFrame | None,
    daily: list[pd.DataFrame],
) -> pd.DataFrame:
    parts = ([existing] if existing is not None else []) + daily
    if not parts:
        raise ValueError("No kline rows to merge")
    return (
        pd.concat(parts, ignore_index=True)
        .drop_duplicates("open_time", keep="last")
        .sort_values("open_time")
        .reset_index(drop=True)
    )


def build_daily_symbol(
    item: DailySymbolArchives,
    output: Path,
    retries: int,
    verify: bool,
    cached_record: dict[str, Any] | None,
    force: bool,
) -> dict[str, Any]:
    target = output / "parquet" / f"{item.symbol}.parquet"
    if cached_record and target.exists() and not force:
        return {**cached_record, "cached": True}
    session = requests.Session()
    daily: list[pd.DataFrame] = []
    for key in item.archives:
        url = f"{ARCHIVE_BASE}/{key}"
        response = request_with_retry(session, url, retries=retries)
        if verify:
            verify_checksum(session, url, response.content, retries)
        daily.append(parse_kline_archive(response.content, item.symbol))
    existing = pd.read_parquet(target) if target.exists() else None
    frame = merge_daily_frame(existing, daily)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".daily.tmp.parquet")
    frame.to_parquet(temporary, index=False, compression="zstd")
    temporary.replace(target)
    base = item.base_record or {}
    first_open_time = int(frame.open_time.min())
    first_archive_month = str(
        base.get("first_archive_month")
        or pd.Timestamp(first_open_time, unit="ms", tz="UTC").strftime("%Y-%m")
    )
    metadata = item.current_metadata or {}
    return {
        "symbol": item.symbol,
        "first_archive_month": first_archive_month,
        "archive_months": sorted(
            set(base.get("archive_months", []))
            | {date_from_key(key)[:7] for key in item.archives}
        ),
        "archive_dates": [date_from_key(key) for key in item.archives],
        "rows": int(len(frame)),
        "first_open_time": first_open_time,
        "last_open_time": int(frame.open_time.max()),
        "cached": False,
        "current_status": metadata.get("status"),
        "current_underlying_type": metadata.get("underlyingType"),
        "current_contract_type": metadata.get("contractType"),
    }


def main() -> None:
    args = parse_args()
    dates = date_range(args.start_date, args.end_date)
    args.output.mkdir(parents=True, exist_ok=True)
    selected_dates = set(dates)
    cached_records = load_cached_records(args.output, selected_dates)
    discovered = discover_daily_archives(
        args.output,
        dates,
        args.workers,
        args.retries,
    )
    print(
        json.dumps(
            {
                "status": "discovered",
                "symbols": len(discovered),
                "archives": sum(len(item.archives) for item in discovered),
                "dates": [args.start_date, args.end_date],
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
                build_daily_symbol,
                item,
                args.output,
                args.retries,
                not args.no_checksum,
                cached_records.get(item.symbol),
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
        "source": "Binance official data.binance.vision USD-M daily 1h klines",
        "s3_bucket": S3_BUCKET,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "checksum_verified": not args.no_checksum,
        "symbols": sorted(records, key=lambda item: item["symbol"]),
        "failures": sorted(failures, key=lambda item: item["symbol"]),
    }
    (args.output / EXTENSION_MANIFEST).write_text(
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
