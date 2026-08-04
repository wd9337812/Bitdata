from __future__ import annotations

import argparse
import gzip
import io
import json
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data" / "research" / "bybit_trades_10s"
BASE_URL = "https://public.bybit.com/trading"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download Bybit official daily trade CSVs and aggregate to 10s bars "
            "aligned with Binance aggTrades 10s buckets."
        )
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--symbols", nargs="+", default=["SOLUSDT", "DOGEUSDT", "ADAUSDT"])
    parser.add_argument("--start", default="2026-06-01")
    parser.add_argument("--end", default="2026-06-30")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--retries", type=int, default=4)
    return parser.parse_args()


def download_day(
    session: requests.Session,
    symbol: str,
    day: str,
    retries: int,
    output: Path,
) -> tuple[str, str]:
    url = f"{BASE_URL}/{symbol}/{symbol}{day}.csv.gz"
    key = f"{symbol}:{day}"
    for attempt in range(max(1, retries)):
        try:
            response = session.get(url, timeout=120)
            if response.status_code == 404:
                return key, "missing"
            response.raise_for_status()
            target = output / "raw" / symbol / f"{symbol}{day}.csv.gz"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(response.content)
            return key, "ok"
        except requests.exceptions.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return key, "missing"
            if attempt + 1 < retries:
                time.sleep(min(2**attempt, 8))
        except Exception:
            if attempt + 1 < retries:
                time.sleep(min(2**attempt, 8))
    return key, "failed"


def aggregate_day(path: Path, symbol: str, day: str) -> pd.DataFrame:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        frame = pd.read_csv(handle)
    frame = frame[["timestamp", "price", "size"]].copy()
    frame["timestamp"] = pd.to_numeric(frame["timestamp"], errors="coerce")
    frame["price"] = pd.to_numeric(frame["price"], errors="coerce")
    frame["size"] = pd.to_numeric(frame["size"], errors="coerce")
    frame = frame.dropna(subset=["timestamp", "price"])
    frame["bin_ms"] = (frame.timestamp * 1000).astype("int64") // 10_000 * 10_000
    agg = (
        frame.groupby("bin_ms", as_index=False)
        .agg(
            open=("price", "first"),
            high=("price", "max"),
            low=("price", "min"),
            close=("price", "last"),
            volume=("size", "sum"),
            trades=("size", "count"),
        )
        .sort_values("bin_ms")
        .reset_index(drop=True)
    )
    agg["symbol"] = symbol
    agg["day"] = day
    return agg[
        ["symbol", "day", "bin_ms", "open", "high", "low", "close", "volume", "trades"]
    ]


def main() -> None:
    args = parse_args()
    days = [
        stamp.strftime("%Y-%m-%d")
        for stamp in pd.date_range(args.start, args.end, freq="D")
    ]
    args.output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.exists()
        else {"completed": {}, "summary": {}}
    )
    tasks = [
        (symbol, day)
        for symbol in args.symbols
        for day in days
        if f"{symbol}:{day}" not in manifest["completed"]
    ]
    print(json.dumps({"tasks": len(tasks), "total": len(args.symbols) * len(days)}), flush=True)
    session = requests.Session()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(download_day, session, symbol, day, args.retries, args.output): (
                symbol,
                day,
            )
            for symbol, day in tasks
        }
        for future in as_completed(futures):
            key, status = future.result()
            symbol, day = key.split(":")
            if status != "ok":
                manifest.setdefault("failed", {})[key] = status
                print(f"failed {key}: {status}", flush=True)
                continue
            path = args.output / "raw" / symbol / f"{symbol}{day}.csv.gz"
            try:
                agg = aggregate_day(path, symbol, day)
                target = args.output / "parquet" / symbol / f"{day}.parquet"
                target.parent.mkdir(parents=True, exist_ok=True)
                agg.to_parquet(target, index=False)
                manifest["completed"][key] = {
                    "rows": int(len(agg)),
                    "first_bin_ms": int(agg.bin_ms.min()),
                    "last_bin_ms": int(agg.bin_ms.max()),
                }
                path.unlink(missing_ok=True)
            except Exception as exc:
                manifest.setdefault("failed", {})[key] = str(exc)
                print(f"aggregate failed {key}: {exc}", flush=True)
            manifest["summary"] = {
                "source": "https://public.bybit.com/trading",
                "completed": len(manifest["completed"]),
            }
            tmp = manifest_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(manifest_path)
            if len(manifest["completed"]) % 10 == 0:
                print(
                    f"completed {len(manifest['completed'])}/{len(tasks)}",
                    flush=True,
                )
    for raw_dir in (args.output / "raw").glob("*"):
        shutil.rmtree(raw_dir, ignore_errors=True)
    print(
        json.dumps(
            {
                "completed": len(manifest["completed"]),
                "failed": len(manifest.get("failed", {})),
                "output": str(args.output),
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
