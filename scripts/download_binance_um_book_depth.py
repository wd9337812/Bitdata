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

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://data.binance.vision/data/futures/um/daily/bookDepth"
DEFAULT_SOURCE = ROOT / "data" / "research" / "s0_public_1m" / "s0_candidates_1m.parquet"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "binance_um_book_depth"
BANDS = (0.2, 1.0, 5.0)


@dataclass(frozen=True)
class Task:
    symbol: str
    day: str
    url: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aggregate official Binance USD-M 30-second depth bands into trailing 5m features."
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--symbol-workers", type=int, default=6)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--symbols", nargs="*", default=None)
    return parser.parse_args()


def days_for_range(start_ms: int, end_ms: int) -> list[str]:
    start = pd.to_datetime(start_ms, unit="ms", utc=True).floor("D") - pd.Timedelta(days=1)
    end = pd.to_datetime(end_ms, unit="ms", utc=True).floor("D")
    return [stamp.strftime("%Y-%m-%d") for stamp in pd.date_range(start, end, freq="D")]


def parse_archive(content: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"Expected one depth CSV, got {names!r}")
        with archive.open(names[0]) as handle:
            frame = pd.read_csv(
                handle,
                usecols=["timestamp", "percentage", "notional"],
            )
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    frame["percentage"] = pd.to_numeric(frame["percentage"], errors="coerce")
    frame["notional"] = pd.to_numeric(frame["notional"], errors="coerce")
    return frame.dropna()


def _imbalance(bid: pd.Series, ask: pd.Series) -> pd.Series:
    denominator = bid + ask
    return (bid - ask) / denominator.where(denominator.gt(0))


def aggregate_depth(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    pivot = (
        frame.pivot_table(
            index="timestamp",
            columns="percentage",
            values="notional",
            aggfunc="last",
        )
        .sort_index()
        .ffill(limit=2)
    )
    snapshots = pd.DataFrame(index=pivot.index)
    for band in BANDS:
        missing = pd.Series(np.nan, index=pivot.index, dtype="float64")
        bid = pd.to_numeric(pivot.get(-band, missing), errors="coerce")
        ask = pd.to_numeric(pivot.get(band, missing), errors="coerce")
        snapshots[f"book_bid_{band:g}"] = bid
        snapshots[f"book_ask_{band:g}"] = ask
        snapshots[f"book_imbalance_{band:g}"] = _imbalance(bid, ask)
        snapshots[f"book_depth_log_{band:g}"] = np.log1p(
            (bid + ask).clip(lower=0)
        )
    snapshots["book_near_share"] = (
        snapshots["book_bid_0.2"] + snapshots["book_ask_0.2"]
    ) / (
        snapshots["book_bid_5"] + snapshots["book_ask_5"]
    ).replace(0, np.nan)
    snapshots["book_near_band_available"] = (
        snapshots["book_bid_0.2"].notna() & snapshots["book_ask_0.2"].notna()
    ).astype("int8")
    snapshots["bucket"] = snapshots.index.floor("5min")
    grouped = snapshots.groupby("bucket", sort=True)
    result = pd.DataFrame(index=grouped.size().index)
    result["book_snapshot_count"] = grouped.size().astype("int16")
    for band in BANDS:
        key = f"book_imbalance_{band:g}"
        stats = grouped[key].agg(["mean", "last"])
        result[f"{key}_mean"] = stats["mean"]
        result[f"{key}_last"] = stats["last"]
        result[f"{key}_std"] = grouped[key].std(ddof=0).fillna(0.0)
        signed = np.sign(snapshots[key])
        result[f"{key}_persistence"] = signed.groupby(snapshots["bucket"]).mean()
        result[f"book_depth_log_{band:g}_mean"] = grouped[
            f"book_depth_log_{band:g}"
        ].mean()
    result["book_near_share_mean"] = grouped.book_near_share.mean()
    result["book_near_band_available"] = grouped.book_near_band_available.mean()
    near = grouped["book_imbalance_0.2"].agg(["first", "last", "count"])
    result["book_imbalance_0.2_slope"] = (
        near["last"] - near["first"]
    ).where(near["count"].ge(2))
    result.insert(
        0,
        "available_ms",
        (
            result.index.tz_convert("UTC").tz_localize(None).to_numpy(dtype="datetime64[ms]")
            .astype("int64")
            + 300_000
        ),
    )
    return result.reset_index(drop=True).replace([np.inf, -np.inf], np.nan)


def _download(task: Task, retries: int) -> tuple[pd.DataFrame | None, str | None]:
    error: str | None = None
    for attempt in range(max(1, retries)):
        try:
            response = requests.get(task.url, timeout=60)
            if response.status_code == 404:
                return None, None
            response.raise_for_status()
            return aggregate_depth(parse_archive(response.content)), None
        except Exception as exc:
            error = str(exc)
            if attempt + 1 < retries:
                time.sleep(0.5 * (2**attempt))
    return None, error


def _existing_coverage(path: Path, symbol: str, days: int) -> dict[str, Any] | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    frame = pd.read_parquet(path, columns=["available_ms"])
    if frame.empty:
        return None
    return {
        "symbol": symbol,
        "days_requested": days,
        "days_found": None,
        "rows": int(len(frame)),
        "start_ms": int(frame.available_ms.min()),
        "end_ms": int(frame.available_ms.max()),
        "cached": True,
    }


def _process_symbol(
    symbol: str,
    start_ms: int,
    end_ms: int,
    output: Path,
    workers: int,
    retries: int,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    days = days_for_range(start_ms, end_ms)
    output_path = output / f"{symbol}-book-depth.parquet"
    existing = pd.DataFrame()
    present_days: set[str] = set()
    if output_path.exists() and output_path.stat().st_size:
        existing = pd.read_parquet(output_path)
        if not existing.empty:
            if "book_near_band_available" not in existing:
                existing["book_near_band_available"] = 1.0
            timestamps = pd.to_datetime(
                existing.available_ms - 300_000,
                unit="ms",
                utc=True,
            )
            present_days = set(timestamps.dt.strftime("%Y-%m-%d"))
    missing_days = [day for day in days if day not in present_days]
    if not missing_days:
        return {
            "symbol": symbol,
            "days_requested": len(days),
            "days_found": len(present_days),
            "rows": int(len(existing)),
            "start_ms": int(existing.available_ms.min()),
            "end_ms": int(existing.available_ms.max()),
            "cached": True,
        }, []
    tasks = [
        Task(
            symbol=symbol,
            day=day,
            url=f"{BASE_URL}/{symbol}/{symbol}-bookDepth-{day}.zip",
        )
        for day in missing_days
    ]
    parts: list[pd.DataFrame] = []
    errors: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(_download, task, retries): task
            for task in tasks
        }
        for future in as_completed(futures):
            task = futures[future]
            frame, error = future.result()
            if frame is not None and not frame.empty:
                parts.append(frame)
            if error:
                errors.append({"symbol": task.symbol, "day": task.day, "error": error})
    if parts or not existing.empty:
        combined = (
            pd.concat([existing, *parts], ignore_index=True)
            .drop_duplicates("available_ms", keep="last")
            .sort_values("available_ms")
        )
    else:
        combined = pd.DataFrame(columns=["available_ms"])
    if "symbol" in combined:
        combined["symbol"] = symbol
        columns = ["available_ms", "symbol", *[
            column for column in combined.columns
            if column not in {"available_ms", "symbol"}
        ]]
        combined = combined[columns]
    else:
        combined.insert(1, "symbol", symbol)
    timestamps = pd.to_datetime(
        combined.available_ms - 300_000,
        unit="ms",
        utc=True,
    )
    found_days = set(timestamps.dt.strftime("%Y-%m-%d"))
    combined.to_parquet(output_path, index=False)
    coverage = {
        "symbol": symbol,
        "days_requested": len(days),
        "days_found": len(found_days),
        "rows": int(len(combined)),
        "start_ms": int(combined.available_ms.min()) if len(combined) else None,
        "end_ms": int(combined.available_ms.max()) if len(combined) else None,
        "cached": False,
    }
    return coverage, errors


def main() -> None:
    args = parse_args()
    source = pd.read_parquet(args.source, columns=["open_time", "symbol"])
    source["symbol"] = source.symbol.astype(str).str.upper()
    if args.symbols:
        allowed = {symbol.upper() for symbol in args.symbols}
        source = source[source.symbol.isin(allowed)]
    grouped = source.groupby("symbol").open_time.agg(["min", "max"])
    args.output.mkdir(parents=True, exist_ok=True)
    coverage: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    ranges = [
        (str(symbol), int(row["min"]), int(row["max"]))
        for symbol, row in grouped.iterrows()
    ]
    completed = 0
    with ThreadPoolExecutor(max_workers=max(1, args.symbol_workers)) as pool:
        futures = {
            pool.submit(
                _process_symbol,
                symbol,
                start_ms,
                end_ms,
                args.output,
                args.workers,
                args.retries,
            ): symbol
            for symbol, start_ms, end_ms in ranges
        }
        for future in as_completed(futures):
            item, item_errors = future.result()
            coverage.append(item)
            errors.extend(item_errors)
            completed += 1
            found = "cached" if item.get("cached") else item.get("days_found")
            print(
                f"[{completed}/{len(grouped)}] {item['symbol']}: "
                f"{found}/{item['days_requested']} days, {item['rows']} rows"
            )
    manifest = {
        "source": str(args.source),
        "symbols": len(grouped),
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
                "symbols": len(grouped),
                "errors": len(errors),
                "rows": sum(int(item["rows"]) for item in coverage),
                "output": str(args.output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
