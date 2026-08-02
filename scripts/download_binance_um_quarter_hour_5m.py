from __future__ import annotations

import argparse
import hashlib
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
BASE_URL = "https://data.binance.vision/data/futures/um/monthly/klines"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "binance_um_quarter_hour_5m"
DEFAULT_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "XRPUSDT",
    "SOLUSDT",
    "DOGEUSDT",
    "ADAUSDT",
)
KLINE_COLUMNS = (
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
)
RESEARCH_COLUMNS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "close_time",
    "quote_volume",
    "count",
    "taker_buy_quote_volume",
)


@dataclass(frozen=True)
class Task:
    symbol: str
    month: str

    @property
    def filename(self) -> str:
        return f"{self.symbol}-5m-{self.month}.zip"

    @property
    def url(self) -> str:
        return f"{BASE_URL}/{self.symbol}/5m/{self.filename}"

    @property
    def checksum_url(self) -> str:
        return f"{self.url}.CHECKSUM"

    def target(self, output: Path) -> Path:
        return output / "parquet" / self.symbol / f"{self.month}.parquet"


def parse_checksum(payload: str) -> str:
    digest = payload.strip().split()[0].lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("Invalid Binance SHA-256 checksum")
    return digest


def read_archive(content: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"Expected one CSV, got {names!r}")
        payload = archive.read(names[0])
    frame = pd.read_csv(io.BytesIO(payload))
    if set(KLINE_COLUMNS) - set(frame.columns):
        frame = pd.read_csv(io.BytesIO(payload), header=None)
        if frame.shape[1] != len(KLINE_COLUMNS):
            raise ValueError(f"Expected {len(KLINE_COLUMNS)} kline columns")
        frame.columns = KLINE_COLUMNS
    result = frame.loc[:, RESEARCH_COLUMNS].copy()
    for column in RESEARCH_COLUMNS:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    return result.dropna().sort_values("open_time").reset_index(drop=True)


def tasks(symbols: list[str], start: str, end: str) -> list[Task]:
    months = pd.period_range(start=start, end=end, freq="M")
    return [Task(symbol.upper(), str(month)) for symbol in symbols for month in months]


def download(
    task: Task, retries: int
) -> tuple[Task, pd.DataFrame | None, str | None, str | None]:
    error: str | None = None
    for attempt in range(max(1, retries)):
        try:
            archive = requests.get(task.url, timeout=90)
            if archive.status_code == 404:
                return task, None, None, None
            archive.raise_for_status()
            checksum = requests.get(task.checksum_url, timeout=30)
            checksum.raise_for_status()
            expected = parse_checksum(checksum.text)
            actual = hashlib.sha256(archive.content).hexdigest()
            if actual != expected:
                raise ValueError(f"Checksum mismatch: expected {expected}, got {actual}")
            return task, read_archive(archive.content), actual, None
        except Exception as exc:
            error = str(exc)
            if attempt + 1 < retries:
                time.sleep(0.5 * (2**attempt))
    return task, None, None, error


def run(
    symbols: list[str],
    start: str,
    end: str,
    output: Path,
    workers: int,
    retries: int,
) -> dict[str, Any]:
    all_tasks = tasks(symbols, start, end)
    pending = [task for task in all_tasks if not task.target(output).exists()]
    missing = 0
    written = 0
    rows = 0
    errors: list[dict[str, str]] = []
    checksums: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [executor.submit(download, task, retries) for task in pending]
        for future in as_completed(futures):
            task, frame, checksum, error = future.result()
            if error:
                errors.append(
                    {"symbol": task.symbol, "month": task.month, "error": error}
                )
                continue
            if frame is None:
                missing += 1
                continue
            target = task.target(output)
            target.parent.mkdir(parents=True, exist_ok=True)
            staged = target.with_suffix(".parquet.tmp")
            frame.to_parquet(staged, index=False)
            staged.replace(target)
            written += 1
            rows += len(frame)
            checksums[f"{task.symbol}/{task.month}"] = str(checksum)
    report: dict[str, Any] = {
        "source": "Binance official USD-M monthly 5m kline archive",
        "start": start,
        "end": end,
        "symbols": [symbol.upper() for symbol in symbols],
        "tasks": len(all_tasks),
        "cached": len(all_tasks) - len(pending),
        "written": written,
        "rows": rows,
        "missing": missing,
        "errors": errors,
        "sha256_verified_files": len(checksums),
        "cached_not_reverified_this_run": len(all_tasks) - len(pending),
        "sha256_verified_this_run": len(checksums),
        "checksums": checksums,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download checksum-verified 5m klines with taker-buy volume."
    )
    parser.add_argument("--start", default="2021-01")
    parser.add_argument("--end", default="2026-06")
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--retries", type=int, default=4)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    print(
        json.dumps(
            run(args.symbols, args.start, args.end, args.output, args.workers, args.retries),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
