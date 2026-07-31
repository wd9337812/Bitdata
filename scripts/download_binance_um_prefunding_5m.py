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
BASE_URL = "https://data.binance.vision/data/futures/um/monthly"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "binance_um_prefunding_5m"
KINDS = ("premiumIndexKlines", "fundingRate", "klines")
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


@dataclass(frozen=True)
class Task:
    kind: str
    symbol: str
    month: str

    @property
    def filename(self) -> str:
        if self.kind == "fundingRate":
            return f"{self.symbol}-fundingRate-{self.month}.zip"
        return f"{self.symbol}-5m-{self.month}.zip"

    @property
    def url(self) -> str:
        if self.kind == "fundingRate":
            return f"{BASE_URL}/{self.kind}/{self.symbol}/{self.filename}"
        return f"{BASE_URL}/{self.kind}/{self.symbol}/5m/{self.filename}"

    def target(self, output: Path) -> Path:
        return output / self.kind / self.symbol / f"{self.month}.parquet"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download compact point-in-time data for pre-funding audits."
    )
    parser.add_argument("--start", required=True, help="First month, YYYY-MM")
    parser.add_argument("--end", required=True, help="Last month, YYYY-MM")
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--retries", type=int, default=4)
    return parser.parse_args()


def tasks(symbols: list[str], start: str, end: str) -> list[Task]:
    months = pd.period_range(start=start, end=end, freq="M")
    return [
        Task(kind, symbol.upper(), str(month))
        for symbol in symbols
        for month in months
        for kind in KINDS
    ]


def read_archive(kind: str, content: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"Expected one CSV, got {names!r}")
        payload = archive.read(names[0])
    frame = pd.read_csv(io.BytesIO(payload))
    if kind == "fundingRate":
        required = {"calc_time", "funding_interval_hours", "last_funding_rate"}
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"Missing funding columns: {sorted(missing)}")
        result = frame.loc[:, sorted(required)].copy()
        for column in required:
            result[column] = pd.to_numeric(result[column], errors="coerce")
        return result.dropna().sort_values("calc_time").reset_index(drop=True)
    missing = set(KLINE_COLUMNS) - set(frame.columns)
    if missing:
        # Older Binance archives have the same 12 fields but omit the header row.
        headerless = pd.read_csv(io.BytesIO(payload), header=None)
        if headerless.shape[1] != len(KLINE_COLUMNS):
            raise ValueError(f"Missing kline columns: {sorted(missing)}")
        headerless.columns = KLINE_COLUMNS
        frame = headerless
    result = frame.loc[
        :, ["open_time", "open", "high", "low", "close", "close_time", "quote_volume"]
    ].copy()
    for column in result.columns:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    return result.dropna().sort_values("open_time").reset_index(drop=True)


def download(task: Task, retries: int) -> tuple[Task, pd.DataFrame | None, str | None]:
    error: str | None = None
    for attempt in range(max(1, retries)):
        try:
            response = requests.get(task.url, timeout=60)
            if response.status_code == 404:
                return task, None, None
            response.raise_for_status()
            return task, read_archive(task.kind, response.content), None
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
    all_tasks = tasks(symbols, start, end)
    pending = [task for task in all_tasks if not task.target(output).exists()]
    missing = 0
    errors: list[dict[str, str]] = []
    written = 0
    rows = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [executor.submit(download, task, retries) for task in pending]
        for future in as_completed(futures):
            task, frame, error = future.result()
            if error:
                errors.append(
                    {"kind": task.kind, "symbol": task.symbol, "month": task.month, "error": error}
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
    report: dict[str, Any] = {
        "start": start,
        "end": end,
        "symbols": [symbol.upper() for symbol in symbols],
        "tasks": len(all_tasks),
        "cached": len(all_tasks) - len(pending),
        "written": written,
        "rows": rows,
        "missing": missing,
        "errors": errors,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


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
