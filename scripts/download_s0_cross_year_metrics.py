from __future__ import annotations

import argparse
import io
import json
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://data.binance.vision/data/futures/um/daily/metrics"
DEFAULT_FUNDING = ROOT / "data" / "research" / "binance_um_point_in_time_funding"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "binance_um_metrics_cross_year"
RAW_COLUMNS = (
    "create_time",
    "sum_open_interest",
    "sum_open_interest_value",
    "count_toptrader_long_short_ratio",
    "sum_toptrader_long_short_ratio",
    "count_long_short_ratio",
    "sum_taker_long_short_vol_ratio",
)


def top_symbols(funding_dir: Path, top_n: int) -> list[str]:
    counts: dict[str, int] = {}
    for path in funding_dir.glob("*-funding.parquet"):
        try:
            frame = pd.read_parquet(path, columns=["timestamp_ms"])
            counts[path.name.split("-funding.parquet")[0].upper()] = len(frame)
        except Exception:
            continue
    return [
        symbol
        for symbol, _ in sorted(counts.items(), key=lambda item: item[1], reverse=True)[
            :top_n
        ]
    ]


def download_day(
    session: requests.Session,
    symbol: str,
    day: str,
    retries: int,
) -> pd.DataFrame | None:
    url = f"{BASE_URL}/{symbol}/{symbol}-metrics-{day}.zip"
    for attempt in range(max(1, retries)):
        try:
            response = session.get(url, timeout=30)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
                if len(names) != 1:
                    return None
                with archive.open(names[0]) as handle:
                    frame = pd.read_csv(handle)
            missing = set(RAW_COLUMNS) - set(frame.columns)
            if missing:
                return None
            return frame
        except Exception:
            if attempt + 1 < retries:
                time.sleep(min(2**attempt, 8))
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--funding", type=Path, default=DEFAULT_FUNDING)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--top", type=int, default=100)
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default="2026-08-06")
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--symbols", nargs="*", default=None)
    args = parser.parse_args()

    symbols = (
        [symbol.upper() for symbol in args.symbols]
        if args.symbols
        else top_symbols(args.funding, args.top)
    )
    days = [
        stamp.strftime("%Y-%m-%d")
        for stamp in pd.date_range(args.start, args.end, freq="D", tz="UTC")
    ]
    args.output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.exists()
        else {"completed": {}, "failures": []}
    )
    print(
        json.dumps(
            {"symbols": len(symbols), "days": len(days), "tasks": len(symbols) * len(days)},
            ensure_ascii=False,
        ),
        flush=True,
    )
    session = requests.Session()
    for symbol in symbols:
        if symbol in manifest.get("completed", {}):
            continue
        parts: list[pd.DataFrame] = []
        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {
                pool.submit(download_day, session, symbol, day, args.retries): day
                for day in days
            }
            for future in as_completed(futures):
                day = futures[future]
                frame = future.result()
                if frame is not None:
                    parts.append(frame)
                else:
                    failures.append(day)
        if parts:
            combined = pd.concat(parts, ignore_index=True)
            combined["timestamp_ms"] = (
                pd.to_datetime(combined["create_time"], utc=True, errors="coerce")
                .dt.as_unit("ns")
                .astype("int64")
                // 1_000_000
            )
            combined = combined.dropna(subset=["timestamp_ms"])
            numeric = [column for column in RAW_COLUMNS if column != "create_time"]
            combined[numeric] = combined[numeric].apply(pd.to_numeric, errors="coerce")
            combined = (
                combined.sort_values("timestamp_ms")
                .drop_duplicates("timestamp_ms", keep="last")
                .reset_index(drop=True)
            )
            combined.to_parquet(
                args.output / f"{symbol}-metrics.parquet",
                index=False,
            )
        manifest["completed"][symbol] = {
            "rows": sum(len(part) for part in parts),
            "downloaded_days": len(parts),
            "missing_days": len(failures),
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(
            f"done {symbol}: rows={manifest['completed'][symbol]['rows']} "
            f"downloaded={manifest['completed'][symbol]['downloaded_days']} "
            f"missing={manifest['completed'][symbol]['missing_days']} "
            f"completed={len(manifest['completed'])}/{len(symbols)}",
            flush=True,
        )
    total_rows = sum(
        int(item["rows"]) for item in manifest["completed"].values()
    )
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
