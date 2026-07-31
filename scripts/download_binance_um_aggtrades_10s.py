from __future__ import annotations

import argparse
import json
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

try:
    from scripts.download_binance_um_aggtrades import RAW_COLUMNS, _boolean, _read_chunks
except ModuleNotFoundError:  # Support direct execution from the repository root.
    from download_binance_um_aggtrades import RAW_COLUMNS, _boolean, _read_chunks


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://data.binance.vision/data/futures/um/daily/aggTrades"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "binance_um_aggtrades_10s"


@dataclass(frozen=True)
class DailyTask:
    symbol: str
    date: str

    @property
    def filename(self) -> str:
        return f"{self.symbol}-aggTrades-{self.date}.zip"

    @property
    def url(self) -> str:
        return f"{BASE_URL}/{self.symbol}/{self.filename}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Stream official Binance USD-M aggTrades archives into exact 10-second "
            "bars. Raw archives are deleted after each daily feature file is written."
        )
    )
    parser.add_argument("--start", required=True, help="Inclusive UTC date (YYYY-MM-DD).")
    parser.add_argument("--end", required=True, help="Inclusive UTC date (YYYY-MM-DD).")
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--retries", type=int, default=4)
    return parser.parse_args()


def daily_tasks(symbols: list[str], start: str, end: str) -> list[DailyTask]:
    dates = pd.date_range(start, end, freq="D", tz="UTC")
    return [
        DailyTask(symbol.upper(), date.strftime("%Y-%m-%d"))
        for symbol in symbols
        for date in dates
    ]


def aggregate_chunk_10s(frame: pd.DataFrame) -> pd.DataFrame:
    numeric = frame.loc[:, RAW_COLUMNS].copy()
    for column in ("price", "quantity", "transact_time"):
        numeric[column] = pd.to_numeric(numeric[column], errors="coerce")
    numeric = numeric.dropna(subset=["price", "quantity", "transact_time"])
    if numeric.empty:
        return pd.DataFrame()

    numeric["bin_ms"] = (
        numeric.transact_time.astype("int64").floordiv(10_000).mul(10_000)
    )
    numeric["quote"] = numeric.price * numeric.quantity
    numeric["taker_buy"] = ~_boolean(numeric.is_buyer_maker)
    numeric["signed_quantity"] = numeric.quantity.where(
        numeric.taker_buy, -numeric.quantity
    )
    numeric["signed_quote"] = numeric.quote.where(numeric.taker_buy, -numeric.quote)
    grouped = numeric.groupby("bin_ms", sort=True)
    return grouped.agg(
        trade_count=("price", "size"),
        quantity=("quantity", "sum"),
        quote=("quote", "sum"),
        signed_quantity=("signed_quantity", "sum"),
        signed_quote=("signed_quote", "sum"),
        open=("price", "first"),
        high=("price", "max"),
        low=("price", "min"),
        close=("price", "last"),
    ).reset_index()


def combine_partials_10s(parts: list[pd.DataFrame]) -> pd.DataFrame:
    usable = [part for part in parts if not part.empty]
    if not usable:
        return pd.DataFrame()
    frame = pd.concat(usable, ignore_index=True).sort_values("bin_ms", kind="stable")
    grouped = frame.groupby("bin_ms", sort=True)
    result = grouped[
        ["trade_count", "quantity", "quote", "signed_quantity", "signed_quote"]
    ].sum()
    result["open"] = grouped.open.first()
    result["high"] = grouped.high.max()
    result["low"] = grouped.low.min()
    result["close"] = grouped.close.last()
    return result.reset_index()


def finalize_10s(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    result = frame.sort_values("bin_ms").copy()
    result.insert(1, "available_ms", result.bin_ms.astype("int64") + 10_000)
    result["order_imbalance"] = (
        result.signed_quantity / result.quantity.replace(0.0, np.nan)
    ).clip(-1.0, 1.0)
    result["quote_imbalance"] = (
        result.signed_quote / result.quote.replace(0.0, np.nan)
    ).clip(-1.0, 1.0)
    return result.replace([np.inf, -np.inf], np.nan)


def aggregate_archive_10s(path: Path) -> pd.DataFrame:
    return finalize_10s(
        combine_partials_10s([aggregate_chunk_10s(chunk) for chunk in _read_chunks(path)])
    )


def download_task(task: DailyTask, output: Path, retries: int) -> dict[str, Any]:
    target = output / task.symbol / f"{task.date}.parquet"
    if target.exists() and target.stat().st_size:
        return {"symbol": task.symbol, "date": task.date, "status": "cached"}
    target.parent.mkdir(parents=True, exist_ok=True)
    error: str | None = None
    for attempt in range(max(1, retries)):
        temp_path: Path | None = None
        try:
            with requests.get(task.url, timeout=180, stream=True) as response:
                if response.status_code == 404:
                    return {"symbol": task.symbol, "date": task.date, "status": "missing"}
                response.raise_for_status()
                with tempfile.NamedTemporaryFile(
                    dir=output,
                    suffix=".zip",
                    delete=False,
                ) as handle:
                    temp_path = Path(handle.name)
                    for block in response.iter_content(chunk_size=1024 * 1024):
                        if block:
                            handle.write(block)
            frame = aggregate_archive_10s(temp_path)
            if frame.empty:
                raise ValueError("Archive contained no usable aggregate trades")
            staged = target.with_suffix(".parquet.tmp")
            frame.to_parquet(staged, index=False)
            staged.replace(target)
            return {
                "symbol": task.symbol,
                "date": task.date,
                "status": "written",
                "rows": int(len(frame)),
                "bytes": int(target.stat().st_size),
            }
        except Exception as exc:
            error = str(exc)
            if attempt + 1 < retries:
                time.sleep(0.75 * (2**attempt))
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
    return {
        "symbol": task.symbol,
        "date": task.date,
        "status": "error",
        "error": error,
    }


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    results = [
        download_task(task, args.output, args.retries)
        for task in daily_tasks(args.symbols, args.start, args.end)
    ]
    summary = {
        "start": args.start,
        "end": args.end,
        "symbols": [symbol.upper() for symbol in args.symbols],
        "tasks": len(results),
        "status_counts": pd.Series([item["status"] for item in results])
        .value_counts()
        .to_dict(),
        "errors": [item for item in results if item["status"] == "error"],
    }
    (args.output / "manifest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
