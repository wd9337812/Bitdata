from __future__ import annotations

import argparse
import calendar
import gzip
import io
import json
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_OUTPUT = ROOT / "data" / "research" / "bybit_mt4_1m"
BASE_URL = "https://public.bybit.com/kline_for_metatrader4"
SYMBOLS = (
    "ADAUSDT", "APEUSDT", "ATOMUSDT", "AVAXUSDT", "AXSUSDT", "BITUSDT",
    "BNBUSDT", "BTCUSDT", "ETCUSDT", "ETHUSDT", "FILUSDT", "FTMUSDT",
    "GMTUSDT", "LINKUSDT", "LTCUSDT", "LUNA2USDT", "MATICUSDT", "NEARUSDT",
    "SANDUSDT", "SOLUSDT", "TRBUSDT", "UNFIUSDT", "XRPUSDT",
)
COLUMNS = ("dt", "open", "high", "low", "close", "volume")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download Bybit official MT4 1m klines (2020-2025), convert timestamps "
            "from UTC+3 to UTC ms, and store one compact parquet per symbol."
        )
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--symbols", nargs="*", default=list(SYMBOLS))
    parser.add_argument("--start-year", type=int, default=2020)
    parser.add_argument("--end-year", type=int, default=2025)
    parser.add_argument("--workers", type=int, default=24)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--binance-data", type=Path)
    return parser.parse_args()


def month_url(symbol: str, year: int, month: int) -> str:
    _, last_day = calendar.monthrange(year, month)
    start = f"{year:04d}-{month:02d}-01"
    end = f"{year:04d}-{month:02d}-{last_day:02d}"
    return (
        f"{BASE_URL}/{symbol}/{year}/"
        f"{symbol}_1_{start}_{end}.csv.gz"
    )


def download_month(
    session: requests.Session,
    symbol: str,
    year: int,
    month: int,
    retries: int,
    output: Path,
) -> tuple[str, str]:
    """Return (month_key, 'ok' | 'missing' | 'failed')."""
    url = month_url(symbol, year, month)
    month_key = f"{year:04d}-{month:02d}"
    for attempt in range(max(1, retries)):
        try:
            response = session.get(url, timeout=60)
            if response.status_code == 404:
                return month_key, "missing"
            response.raise_for_status()
            target = output / "raw" / symbol / f"{symbol}_1_{month_key}.csv.gz"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(response.content)
            return month_key, "ok"
        except requests.exceptions.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return month_key, "missing"
            if attempt + 1 < retries:
                time.sleep(min(2**attempt, 8))
        except Exception:
            if attempt + 1 < retries:
                time.sleep(min(2**attempt, 8))
    return month_key, "failed"


def parse_month(path: Path, symbol: str) -> pd.DataFrame:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        frame = pd.read_csv(
            handle,
            header=None,
            names=COLUMNS,
            dtype={"dt": str},
        )
    frame["dt"] = pd.to_datetime(frame.dt, format="%Y.%m.%d %H:%M", utc=True)
    frame["dt"] = frame.dt - pd.Timedelta(hours=3)
    frame["open_time"] = frame.dt.astype("int64") // 1_000
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["symbol"] = symbol
    return frame[
        ["symbol", "open_time", "open", "high", "low", "close", "volume"]
    ].dropna(subset=["open_time", "open", "close"])


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.exists()
        else {"completed": {}, "summary": {}}
    )
    symbols = [symbol.upper() for symbol in args.symbols]
    months = [
        (year, month)
        for year in range(args.start_year, args.end_year + 1)
        for month in range(1, 13)
    ]
    print(
        json.dumps(
            {
                "symbols": len(symbols),
                "months": len(months),
                "tasks": len(symbols) * len(months),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    session = requests.Session()
    for symbol in symbols:
        if symbol in manifest["completed"]:
            continue
        raw_dir = args.output / "raw" / symbol
        raw_dir.mkdir(parents=True, exist_ok=True)
        results: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {
                pool.submit(
                    download_month,
                    session,
                    symbol,
                    year,
                    month,
                    args.retries,
                    args.output,
                ): (year, month)
                for year, month in months
            }
            for future in as_completed(futures):
                month_key, status = future.result()
                results[month_key] = status
        ok_months = sorted(key for key, status in results.items() if status == "ok")
        missing = sorted(key for key, status in results.items() if status == "missing")
        failed = sorted(key for key, status in results.items() if status == "failed")
        parts: list[pd.DataFrame] = []
        parsed: list[str] = []
        for month_key in ok_months:
            path = raw_dir / f"{symbol}_1_{month_key}.csv.gz"
            if not path.exists():
                continue
            try:
                parts.append(parse_month(path, symbol))
                parsed.append(month_key)
            except Exception as exc:
                print(f"{symbol} parse error {month_key}: {exc}", flush=True)
        entry: dict[str, Any] = {
            "rows": 0,
            "downloaded_months": len(ok_months),
            "missing_months": missing,
            "failed_months": failed,
        }
        if parts:
            combined = (
                pd.concat(parts, ignore_index=True)
                .drop_duplicates("open_time", keep="last")
                .sort_values("open_time")
                .reset_index(drop=True)
            )
            target = args.output / "parquet" / f"{symbol}.parquet"
            target.parent.mkdir(parents=True, exist_ok=True)
            combined.to_parquet(target, index=False)
            entry["rows"] = int(len(combined))
            entry["first_open_time"] = int(combined.open_time.min())
            entry["last_open_time"] = int(combined.open_time.max())
            if args.binance_data is not None:
                binance_path = args.binance_data / f"{symbol}.parquet"
                if binance_path.exists():
                    binance = pd.read_parquet(
                        binance_path, columns=["open_time"]
                    )
                    overlap = int(
                        binance.open_time.isin(combined.open_time).sum()
                    )
                    entry["binance_overlap_rows"] = overlap
                    entry["binance_rows"] = int(len(binance))
            shutil.rmtree(raw_dir, ignore_errors=True)
        if failed:
            entry["status"] = "partial"
            manifest.setdefault("partial", {})[symbol] = entry
            print(
                f"partial {symbol}: ok={len(parsed)} missing={len(missing)} "
                f"failed={failed} -> retry next run",
                flush=True,
            )
        else:
            manifest["completed"][symbol] = entry
            print(
                f"done {symbol}: rows={entry['rows']} ok={len(parsed)} "
                f"missing={len(missing)} completed={len(manifest['completed'])}/{len(symbols)}",
                flush=True,
            )
        manifest["summary"] = {
            "source": "https://public.bybit.com/kline_for_metatrader4 (UTC+3 -> UTC)",
            "completed_symbols": len(manifest["completed"]),
        }
        tmp = manifest_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(manifest_path)
    total_rows = sum(int(item["rows"]) for item in manifest["completed"].values())
    print(
        json.dumps(
            {
                "completed_symbols": len(manifest["completed"]),
                "total_rows": total_rows,
                "output": str(args.output),
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
