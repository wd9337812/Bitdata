from __future__ import annotations

import argparse
import json
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

from scripts.build_binance_um_point_in_time_1h import (  # noqa: E402
    ARCHIVE_BASE,
    parse_kline_archive,
    request_with_retry,
    verify_checksum,
)


DEFAULT_SIGNALS = (
    ROOT
    / "data"
    / "research"
    / "s0_point_in_time_slow_momentum"
    / "selected_signals.parquet"
)
DEFAULT_OUTPUT = ROOT / "data" / "research" / "binance_um_event_1m"


@dataclass(frozen=True)
class EventArchive:
    symbol: str
    date: str
    url: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download checksum-verified Binance USD-M daily 1m archives "
            "only for selected signal execution windows."
        )
    )
    parser.add_argument("--signals", type=Path, default=DEFAULT_SIGNALS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--hold-hours", type=int, default=24)
    parser.add_argument("--workers", type=int, default=24)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--no-checksum", action="store_true")
    return parser.parse_args()


def build_event_archives(
    signals: pd.DataFrame,
    hold_hours: int,
) -> list[EventArchive]:
    tasks: set[tuple[str, str]] = set()
    for row in signals.itertuples(index=False):
        entry = pd.to_datetime(int(row.available_ms), unit="ms", utc=True)
        end = entry + pd.Timedelta(hours=hold_hours, minutes=15)
        for day in pd.date_range(entry.floor("D"), end.floor("D"), freq="D"):
            tasks.add((str(row.symbol), str(day.date())))
    return [
        EventArchive(
            symbol,
            date,
            (
                f"{ARCHIVE_BASE}/data/futures/um/daily/klines/"
                f"{symbol}/1m/{symbol}-1m-{date}.zip"
            ),
        )
        for symbol, date in sorted(tasks)
    ]


def download_event_archive(
    task: EventArchive,
    retries: int,
    checksum: bool,
) -> tuple[EventArchive, pd.DataFrame | None, str | None]:
    session = requests.Session()
    try:
        response = request_with_retry(session, task.url, retries=retries)
        if checksum:
            verify_checksum(session, task.url, response.content, retries)
        return task, parse_kline_archive(response.content, task.symbol), None
    except Exception as exc:
        return task, None, str(exc)


def main() -> None:
    args = parse_args()
    signals = pd.read_parquet(args.signals)
    tasks = build_event_archives(signals, args.hold_hours)
    frames: dict[str, list[pd.DataFrame]] = {}
    dates: dict[str, list[str]] = {}
    failures: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(
                download_event_archive,
                task,
                args.retries,
                not args.no_checksum,
            ): task
            for task in tasks
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            task, frame, error = future.result()
            if frame is not None:
                frames.setdefault(task.symbol, []).append(frame)
                dates.setdefault(task.symbol, []).append(task.date)
            if error:
                failures.append(
                    {"symbol": task.symbol, "date": task.date, "error": error}
                )
            if completed % 100 == 0:
                print(f"downloaded {completed}/{len(tasks)} archives")
    args.output.mkdir(parents=True, exist_ok=True)
    coverage: list[dict[str, Any]] = []
    for symbol, parts in sorted(frames.items()):
        frame = (
            pd.concat(parts, ignore_index=True)
            .drop_duplicates("open_time", keep="last")
            .sort_values("open_time")
        )
        target = args.output / "parquet" / f"{symbol}.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(target, index=False)
        coverage.append(
            {
                "symbol": symbol,
                "dates": sorted(dates[symbol]),
                "rows": int(len(frame)),
            }
        )
    manifest = {
        "source": str(args.signals),
        "timeframe": "1m",
        "hold_hours": args.hold_hours,
        "checksum_verified": not args.no_checksum,
        "tasks": len(tasks),
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
                "rows": sum(item["rows"] for item in coverage),
                "failures": len(failures),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
