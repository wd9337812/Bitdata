from __future__ import annotations

import argparse
import io
import json
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://data.binance.vision/data/futures/um/daily/metrics"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "binance_um_metrics_1h"
RAW_COLUMNS = (
    "create_time",
    "symbol",
    "sum_open_interest",
    "sum_open_interest_value",
    "count_toptrader_long_short_ratio",
    "sum_toptrader_long_short_ratio",
    "count_long_short_ratio",
    "sum_taker_long_short_vol_ratio",
)


@dataclass(frozen=True)
class Task:
    symbol: str
    date: str

    @property
    def filename(self) -> str:
        return f"{self.symbol}-metrics-{self.date}.zip"

    @property
    def url(self) -> str:
        return f"{BASE_URL}/{self.symbol}/{self.filename}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download official Binance USD-M positioning metrics and aggregate to 1h."
    )
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--retries", type=int, default=4)
    return parser.parse_args()


def tasks(symbols: list[str], start: str, end: str) -> list[Task]:
    dates = pd.date_range(start, end, freq="D", tz="UTC")
    return [
        Task(symbol.upper(), date.strftime("%Y-%m-%d"))
        for symbol in symbols
        for date in dates
    ]


def aggregate_metrics(frame: pd.DataFrame) -> pd.DataFrame:
    missing = set(RAW_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(f"Missing metrics columns: {sorted(missing)}")
    result = frame.loc[:, RAW_COLUMNS].copy()
    result["time"] = pd.to_datetime(result.create_time, utc=True, errors="coerce")
    numeric = [column for column in RAW_COLUMNS if column not in {"create_time", "symbol"}]
    result[numeric] = result[numeric].apply(pd.to_numeric, errors="coerce")
    result = result.dropna(subset=["time", "sum_open_interest"])
    if result.empty:
        return pd.DataFrame()
    result["available_time"] = result.time.dt.ceil("h")
    grouped = result.sort_values("time").groupby("available_time", sort=True)
    hourly = grouped[numeric].last().reset_index()
    hourly.insert(
        0,
        "available_ms",
        hourly.available_time.map(lambda value: int(value.timestamp() * 1000)).astype(
            "int64"
        ),
    )
    return hourly.drop(columns="available_time")


def read_archive(content: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"Expected one metrics CSV, got {names!r}")
        with archive.open(names[0]) as handle:
            return aggregate_metrics(pd.read_csv(handle))


def download(task: Task, retries: int) -> tuple[Task, pd.DataFrame | None, str | None]:
    error: str | None = None
    for attempt in range(max(1, retries)):
        try:
            response = requests.get(task.url, timeout=60)
            if response.status_code == 404:
                return task, None, None
            response.raise_for_status()
            return task, read_archive(response.content), None
        except Exception as exc:
            error = str(exc)
            if attempt + 1 < retries:
                time.sleep(0.5 * (2**attempt))
    return task, None, error


def run(
    symbols: list[str],
    start: str,
    end: str,
    output: Path,
    workers: int,
    retries: int,
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    pending: list[Task] = []
    cached: list[str] = []
    for symbol in [item.upper() for item in symbols]:
        target = output / f"{symbol}-metrics.parquet"
        if target.exists() and target.stat().st_size:
            cached.append(symbol)
        else:
            pending.extend(tasks([symbol], start, end))

    frames: dict[str, list[pd.DataFrame]] = {}
    missing = 0
    errors: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [executor.submit(download, task, retries) for task in pending]
        for future in as_completed(futures):
            task, frame, error = future.result()
            if error:
                errors.append({"symbol": task.symbol, "date": task.date, "error": error})
            elif frame is None:
                missing += 1
            else:
                frames.setdefault(task.symbol, []).append(frame)

    written: dict[str, int] = {}
    for symbol, parts in frames.items():
        combined = (
            pd.concat(parts, ignore_index=True)
            .sort_values("available_ms")
            .drop_duplicates("available_ms", keep="last")
        )
        target = output / f"{symbol}-metrics.parquet"
        staged = target.with_suffix(".parquet.tmp")
        combined.to_parquet(staged, index=False)
        staged.replace(target)
        written[symbol] = int(len(combined))

    report: dict[str, Any] = {
        "start": start,
        "end": end,
        "symbols": [item.upper() for item in symbols],
        "tasks": len(pending),
        "cached_symbols": cached,
        "written_rows": written,
        "missing_days": missing,
        "errors": errors,
    }
    (output / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    args = parse_args()
    report = run(
        args.symbols,
        args.start,
        args.end,
        args.output,
        args.workers,
        args.retries,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
