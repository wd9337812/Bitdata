from __future__ import annotations

import argparse
import io
import json
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://data.binance.vision/data/futures/um"
DEFAULT_SOURCE = ROOT / "data" / "research" / "s0_public_1m" / "s0_candidates_1m.parquet"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "binance_um_derivatives"
METRIC_COLUMNS = (
    "create_time",
    "symbol",
    "sum_open_interest",
    "sum_open_interest_value",
    "count_toptrader_long_short_ratio",
    "sum_toptrader_long_short_ratio",
    "count_long_short_ratio",
    "sum_taker_long_short_vol_ratio",
)
FUNDING_COLUMNS = ("calc_time", "funding_interval_hours", "last_funding_rate")


@dataclass(frozen=True)
class DownloadTask:
    symbol: str
    kind: str
    period: str
    url: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download official Binance USD-M historical metrics and funding data."
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workers", type=int, default=24)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--symbols", nargs="*", default=None)
    return parser.parse_args()


def _day_periods(start_ms: int, end_ms: int) -> list[str]:
    start = pd.to_datetime(start_ms, unit="ms", utc=True).floor("D") - pd.Timedelta(days=1)
    end = pd.to_datetime(end_ms, unit="ms", utc=True).floor("D")
    return [stamp.strftime("%Y-%m-%d") for stamp in pd.date_range(start, end, freq="D")]


def _month_periods(start_ms: int, end_ms: int) -> list[str]:
    start = pd.to_datetime(start_ms, unit="ms", utc=True).tz_localize(None).to_period("M") - 1
    end = pd.to_datetime(end_ms, unit="ms", utc=True).tz_localize(None).to_period("M")
    return [str(period) for period in pd.period_range(start, end, freq="M")]


def build_tasks(
    symbol_ranges: dict[str, tuple[int, int]],
) -> list[DownloadTask]:
    tasks: list[DownloadTask] = []
    for symbol, (start_ms, end_ms) in sorted(symbol_ranges.items()):
        for period in _day_periods(start_ms, end_ms):
            filename = f"{symbol}-metrics-{period}.zip"
            tasks.append(
                DownloadTask(
                    symbol=symbol,
                    kind="metrics",
                    period=period,
                    url=f"{BASE_URL}/daily/metrics/{symbol}/{filename}",
                )
            )
        for period in _month_periods(start_ms, end_ms):
            filename = f"{symbol}-fundingRate-{period}.zip"
            tasks.append(
                DownloadTask(
                    symbol=symbol,
                    kind="funding",
                    period=period,
                    url=f"{BASE_URL}/monthly/fundingRate/{symbol}/{filename}",
                )
            )
    return tasks


def parse_archive(content: bytes, kind: str) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"Expected one CSV in archive, got {names!r}")
        with archive.open(names[0]) as handle:
            frame = pd.read_csv(handle)
    expected = set(METRIC_COLUMNS if kind == "metrics" else FUNDING_COLUMNS)
    missing = expected - set(frame.columns)
    if missing:
        raise ValueError(f"Missing {kind} columns: {sorted(missing)}")
    return frame


def _download(task: DownloadTask, retries: int) -> tuple[DownloadTask, pd.DataFrame | None, str | None]:
    error: str | None = None
    for attempt in range(max(1, retries)):
        try:
            response = requests.get(task.url, timeout=30)
            if response.status_code == 404:
                return task, None, None
            response.raise_for_status()
            return task, parse_archive(response.content, task.kind), None
        except Exception as exc:
            error = str(exc)
            if attempt + 1 < retries:
                time.sleep(0.4 * (2**attempt))
    return task, None, error


def _combine_metrics(parts: Iterable[pd.DataFrame], symbol: str) -> pd.DataFrame:
    frames = list(parts)
    if not frames:
        return pd.DataFrame(columns=("timestamp_ms", "symbol", *METRIC_COLUMNS[2:]))
    result = pd.concat(frames, ignore_index=True)
    timestamps = pd.to_datetime(result["create_time"], utc=True, errors="coerce")
    result["timestamp_ms"] = timestamps.dt.as_unit("ns").astype("int64") // 1_000_000
    result["symbol"] = symbol
    numeric = [column for column in METRIC_COLUMNS if column not in {"create_time", "symbol"}]
    result[numeric] = result[numeric].apply(pd.to_numeric, errors="coerce")
    return (
        result.dropna(subset=["timestamp_ms"])
        .drop_duplicates(["timestamp_ms"], keep="last")
        .sort_values("timestamp_ms")[["timestamp_ms", "symbol", *numeric]]
    )


def _combine_funding(parts: Iterable[pd.DataFrame], symbol: str) -> pd.DataFrame:
    frames = list(parts)
    if not frames:
        return pd.DataFrame(columns=("timestamp_ms", "symbol", *FUNDING_COLUMNS[1:]))
    result = pd.concat(frames, ignore_index=True)
    result["timestamp_ms"] = pd.to_numeric(result["calc_time"], errors="coerce")
    result["symbol"] = symbol
    numeric = ["funding_interval_hours", "last_funding_rate"]
    result[numeric] = result[numeric].apply(pd.to_numeric, errors="coerce")
    return (
        result.dropna(subset=["timestamp_ms"])
        .drop_duplicates(["timestamp_ms"], keep="last")
        .sort_values("timestamp_ms")[["timestamp_ms", "symbol", *numeric]]
    )


def main() -> None:
    args = parse_args()
    source = pd.read_parquet(args.source, columns=["open_time", "symbol"])
    source["symbol"] = source["symbol"].astype(str).str.upper()
    if args.symbols:
        allowed = {symbol.upper() for symbol in args.symbols}
        source = source[source.symbol.isin(allowed)]
    grouped = source.groupby("symbol").open_time.agg(["min", "max"])
    symbol_ranges = {
        str(symbol): (int(row["min"]), int(row["max"]))
        for symbol, row in grouped.iterrows()
    }
    tasks = build_tasks(symbol_ranges)
    metrics: dict[str, list[pd.DataFrame]] = {symbol: [] for symbol in symbol_ranges}
    funding: dict[str, list[pd.DataFrame]] = {symbol: [] for symbol in symbol_ranges}
    errors: list[dict[str, str]] = []
    completed = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(_download, task, args.retries): task for task in tasks}
        for future in as_completed(futures):
            task, frame, error = future.result()
            completed += 1
            if frame is not None:
                target = metrics if task.kind == "metrics" else funding
                target[task.symbol].append(frame)
            if error:
                errors.append(
                    {
                        "symbol": task.symbol,
                        "kind": task.kind,
                        "period": task.period,
                        "error": error,
                    }
                )
            if completed % 1000 == 0:
                print(f"downloaded {completed}/{len(tasks)} archives")

    args.output.mkdir(parents=True, exist_ok=True)
    coverage: list[dict[str, object]] = []
    for symbol in sorted(symbol_ranges):
        metric_frame = _combine_metrics(metrics[symbol], symbol)
        funding_frame = _combine_funding(funding[symbol], symbol)
        metric_frame.to_parquet(args.output / f"{symbol}-metrics.parquet", index=False)
        funding_frame.to_parquet(args.output / f"{symbol}-funding.parquet", index=False)
        coverage.append(
            {
                "symbol": symbol,
                "metric_rows": int(len(metric_frame)),
                "funding_rows": int(len(funding_frame)),
                "metric_start_ms": int(metric_frame.timestamp_ms.min()) if len(metric_frame) else None,
                "metric_end_ms": int(metric_frame.timestamp_ms.max()) if len(metric_frame) else None,
                "funding_start_ms": int(funding_frame.timestamp_ms.min()) if len(funding_frame) else None,
                "funding_end_ms": int(funding_frame.timestamp_ms.max()) if len(funding_frame) else None,
            }
        )
    manifest = {
        "source": str(args.source),
        "symbols": len(symbol_ranges),
        "tasks": len(tasks),
        "errors": errors,
        "coverage": coverage,
    }
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "symbols": len(symbol_ranges),
                "tasks": len(tasks),
                "errors": len(errors),
                "metric_rows": sum(int(item["metric_rows"]) for item in coverage),
                "funding_rows": sum(int(item["funding_rows"]) for item in coverage),
                "output": str(args.output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
