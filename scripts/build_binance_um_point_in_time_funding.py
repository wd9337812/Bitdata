from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.download_binance_um_derivatives import (  # noqa: E402
    _combine_funding,
    parse_archive,
)


DEFAULT_SOURCE = (
    ROOT / "data" / "research" / "binance_um_point_in_time_1h"
)
DEFAULT_OUTPUT = (
    ROOT / "data" / "research" / "binance_um_point_in_time_funding"
)
ARCHIVE_BASE = "https://data.binance.vision/data/futures/um"
CHECKSUM_PATTERN = re.compile(r"^([0-9a-fA-F]{64})\s+")


@dataclass(frozen=True)
class FundingTask:
    symbol: str
    period: str
    frequency: str
    url: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build point-in-time Binance USD-M funding data for the same "
            "historical universe as the official 1h archive dataset."
        )
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--no-checksum", action="store_true")
    parser.add_argument(
        "--include-daily",
        action="store_true",
        help=(
            "Request daily funding archives when Binance publishes them. "
            "The archive currently exposes funding data monthly."
        ),
    )
    return parser.parse_args()


def load_tasks(
    source: Path,
    *,
    include_daily: bool = False,
) -> list[FundingTask]:
    manifest = json.loads(
        (source / "manifest.json").read_text(encoding="utf-8")
    )
    daily_path = source / "daily_extension_manifest.json"
    daily = (
        json.loads(daily_path.read_text(encoding="utf-8"))
        if daily_path.exists()
        else {"symbols": []}
    )
    daily_by_symbol = {
        str(item["symbol"]): item.get("archive_dates", [])
        for item in daily.get("symbols", [])
    }
    tasks: list[FundingTask] = []
    for item in manifest["symbols"]:
        symbol = str(item["symbol"])
        for month in item.get("archive_months", []):
            filename = f"{symbol}-fundingRate-{month}.zip"
            tasks.append(
                FundingTask(
                    symbol,
                    str(month),
                    "monthly",
                    f"{ARCHIVE_BASE}/monthly/fundingRate/{symbol}/{filename}",
                )
            )
        if include_daily:
            for day in daily_by_symbol.get(symbol, []):
                filename = f"{symbol}-fundingRate-{day}.zip"
                tasks.append(
                    FundingTask(
                        symbol,
                        str(day),
                        "daily",
                        f"{ARCHIVE_BASE}/daily/fundingRate/{symbol}/{filename}",
                    )
                )
    return tasks


def checksum_from_text(payload: str) -> str:
    match = CHECKSUM_PATTERN.match(payload.strip())
    if not match:
        raise ValueError("Invalid Binance checksum payload")
    return match.group(1).lower()


def request_with_retry(
    session: requests.Session,
    url: str,
    retries: int,
) -> requests.Response | None:
    error: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            response = session.get(url, timeout=45)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            error = exc
            if attempt + 1 < retries:
                time.sleep(min(2**attempt, 8))
    assert error is not None
    raise error


def download_task(
    task: FundingTask,
    retries: int,
    verify_checksum: bool,
) -> tuple[FundingTask, pd.DataFrame | None, str | None]:
    session = requests.Session()
    try:
        response = request_with_retry(session, task.url, retries)
        if response is None:
            return task, None, None
        if verify_checksum:
            checksum_response = request_with_retry(
                session,
                f"{task.url}.CHECKSUM",
                retries,
            )
            if checksum_response is None:
                raise ValueError("Missing Binance checksum")
            expected = checksum_from_text(checksum_response.text)
            actual = hashlib.sha256(response.content).hexdigest()
            if actual != expected:
                raise ValueError(
                    f"Checksum mismatch: expected {expected}, got {actual}"
                )
        return task, parse_archive(response.content, "funding"), None
    except Exception as exc:
        return task, None, str(exc)


def main() -> None:
    args = parse_args()
    tasks = load_tasks(args.source, include_daily=args.include_daily)
    parts: dict[str, list[pd.DataFrame]] = {}
    found: dict[str, list[str]] = {}
    failures: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(
                download_task,
                task,
                args.retries,
                not args.no_checksum,
            ): task
            for task in tasks
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            task, frame, error = future.result()
            if frame is not None:
                parts.setdefault(task.symbol, []).append(frame)
                found.setdefault(task.symbol, []).append(task.period)
            if error:
                failures.append(
                    {
                        "symbol": task.symbol,
                        "period": task.period,
                        "frequency": task.frequency,
                        "error": error,
                    }
                )
            if completed % 1000 == 0:
                print(f"downloaded {completed}/{len(tasks)} archives")

    args.output.mkdir(parents=True, exist_ok=True)
    coverage: list[dict[str, object]] = []
    for symbol in sorted({task.symbol for task in tasks}):
        frame = _combine_funding(parts.get(symbol, []), symbol)
        if not frame.empty:
            frame.to_parquet(
                args.output / f"{symbol}-funding.parquet",
                index=False,
            )
        coverage.append(
            {
                "symbol": symbol,
                "periods": sorted(found.get(symbol, [])),
                "rows": int(len(frame)),
                "first_timestamp_ms": (
                    int(frame.timestamp_ms.min()) if len(frame) else None
                ),
                "last_timestamp_ms": (
                    int(frame.timestamp_ms.max()) if len(frame) else None
                ),
            }
        )
    manifest = {
        "source": str(args.source),
        "archive_base": ARCHIVE_BASE,
        "checksum_verified": not args.no_checksum,
        "tasks": len(tasks),
        "symbols": len(coverage),
        "coverage": coverage,
        "failures": failures,
    }
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "tasks": len(tasks),
                "symbols": len(coverage),
                "symbols_with_data": sum(
                    int(item["rows"] > 0) for item in coverage
                ),
                "rows": sum(int(item["rows"]) for item in coverage),
                "failures": len(failures),
                "output": str(args.output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
