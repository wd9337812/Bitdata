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
BASE_URL = "https://data.binance.vision/data/futures/cm/monthly"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "binance_cm_quarterly_basis_1d"
UNDERLYINGS = ("BTC", "ETH", "BNB", "ADA", "LINK", "BCH", "XRP", "DOT", "LTC")
EXPIRIES = (
    "220325",
    "220624",
    "220930",
    "221230",
    "230331",
    "230630",
    "230929",
    "231229",
    "240329",
    "240628",
    "240927",
    "241227",
    "250328",
    "250627",
    "250926",
    "251226",
    "260327",
    "260626",
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


@dataclass(frozen=True)
class Task:
    kind: str
    symbol: str
    month: str

    @property
    def filename(self) -> str:
        return f"{self.symbol}-1d-{self.month}.zip"

    @property
    def url(self) -> str:
        folder = "indexPriceKlines" if self.kind == "index" else "klines"
        return f"{BASE_URL}/{folder}/{self.symbol}/1d/{self.filename}"

    def target(self, output: Path) -> Path:
        return output / self.kind / self.symbol / f"{self.month}.parquet"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download compact Binance COIN-M current-quarter basis inputs."
    )
    parser.add_argument("--start", default="2022-01", help="First month, YYYY-MM")
    parser.add_argument("--end", default="2026-07", help="Last month, YYYY-MM")
    parser.add_argument("--underlyings", nargs="+", default=list(UNDERLYINGS))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--retries", type=int, default=4)
    return parser.parse_args()


def expiry_timestamp(suffix: str) -> pd.Timestamp:
    return pd.to_datetime(suffix, format="%y%m%d", utc=True)


def active_months(suffix: str, start: str, end: str) -> list[str]:
    expiry = expiry_timestamp(suffix)
    expiry_month = expiry.tz_localize(None).to_period("M")
    # Current-quarter status begins after the previous quarterly delivery.
    first = max(pd.Period(start, freq="M"), expiry_month - 2)
    last = min(pd.Period(end, freq="M"), expiry_month)
    if first > last:
        return []
    return [str(month) for month in pd.period_range(first, last, freq="M")]


def tasks(underlyings: list[str], start: str, end: str) -> list[Task]:
    bases = [base.upper() for base in underlyings]
    result = [
        Task("index", f"{base}USD", str(month))
        for base in bases
        for month in pd.period_range(start, end, freq="M")
    ]
    result.extend(
        Task("delivery", f"{base}USD_{suffix}", month)
        for base in bases
        for suffix in EXPIRIES
        for month in active_months(suffix, start, end)
    )
    return result


def read_archive(content: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"Expected one CSV, got {names!r}")
        payload = archive.read(names[0])
    frame = pd.read_csv(io.BytesIO(payload))
    missing = set(KLINE_COLUMNS) - set(frame.columns)
    if missing:
        headerless = pd.read_csv(io.BytesIO(payload), header=None)
        if headerless.shape[1] != len(KLINE_COLUMNS):
            raise ValueError(f"Missing kline columns: {sorted(missing)}")
        headerless.columns = KLINE_COLUMNS
        frame = headerless
    result = frame.loc[:, ["open_time", "open", "high", "low", "close", "close_time"]].copy()
    for column in result.columns:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    return result.dropna().drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)


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
    underlyings: list[str],
    start: str,
    end: str,
    output: Path,
    workers: int,
    retries: int,
) -> dict[str, Any]:
    all_tasks = tasks(underlyings, start, end)
    pending = [task for task in all_tasks if not task.target(output).exists()]
    missing = 0
    written = 0
    rows = 0
    errors: list[dict[str, str]] = []
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
        "underlyings": [base.upper() for base in underlyings],
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
            run(args.underlyings, args.start, args.end, args.output, args.workers, args.retries),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
