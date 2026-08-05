from __future__ import annotations

import argparse
import gzip
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

DEFAULT_SIGNALS = (
    ROOT / "data" / "research" / "s0_30d_momentum_adaptive_v2"
    / "signals_all.parquet"
)
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_30d_bybit_premium"
DEFAULT_BINANCE = ROOT / "data" / "research" / "binance_um_1m_cross_year" / "parquet"
BASE_URL = "https://public.bybit.com/trading"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build Binance-Bybit premium at 30d-momentum signal entry times using "
            "registration-free Bybit public daily trade archives."
        )
    )
    parser.add_argument("--signals", type=Path, default=DEFAULT_SIGNALS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--binance", type=Path, default=DEFAULT_BINANCE)
    parser.add_argument("--delays", type=int, nargs="+", default=[0, 5, 15])
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument(
        "--aggregate-only",
        action="store_true",
        help="Skip downloads; aggregate whatever raw files already exist.",
    )
    return parser.parse_args()


def download_day(
    session: requests.Session,
    symbol: str,
    day: str,
    retries: int,
    raw_dir: Path,
) -> str:
    """Return 'ok' | 'missing' | 'failed'."""
    url = f"{BASE_URL}/{symbol}/{symbol}{day}.csv.gz"
    target = raw_dir / f"{symbol}{day}.csv.gz"
    if target.exists():
        return "ok"
    for attempt in range(max(1, retries)):
        try:
            response = session.get(url, timeout=180, stream=True)
            if response.status_code == 404:
                return "missing"
            response.raise_for_status()
            with target.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    if chunk:
                        handle.write(chunk)
            return "ok"
        except Exception:
            if attempt + 1 < retries:
                time.sleep(min(2**attempt, 8))
    return "failed"


def aggregate_1m(path: Path) -> pd.DataFrame:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        frame = pd.read_csv(handle)
    frame = frame[["timestamp", "price"]].copy()
    frame["timestamp"] = pd.to_numeric(frame["timestamp"], errors="coerce")
    frame["price"] = pd.to_numeric(frame["price"], errors="coerce")
    frame = frame.dropna(subset=["timestamp", "price"])
    frame["bin_ms"] = (frame.timestamp * 1000).astype("int64") // 60_000 * 60_000
    agg = (
        frame.groupby("bin_ms", as_index=False)["price"]
        .agg(bybit_close="last")
        .sort_values("bin_ms")
        .reset_index(drop=True)
    )
    return agg


def main() -> None:
    args = parse_args()
    signals = pd.read_parquet(args.signals)
    signals = signals[["symbol", "available_ms"]].copy()
    signals["day"] = pd.to_datetime(
        signals.available_ms, unit="ms", utc=True
    ).dt.strftime("%Y-%m-%d")
    args.output.mkdir(parents=True, exist_ok=True)
    raw_dir = args.output / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.exists()
        else {"downloaded": [], "missing": [], "failed": []}
    )
    if not args.aggregate_only:
        tasks = list(
            {
                (row.symbol, row.day)
                for row in signals.itertuples(index=False)
                if f"{row.symbol}:{row.day}" not in manifest["downloaded"]
                and f"{row.symbol}:{row.day}" not in manifest.get("missing", [])
                and f"{row.symbol}:{row.day}" not in manifest.get("failed", [])
            }
        )
        print(json.dumps({"tasks": len(tasks), "signals": len(signals)}), flush=True)
        session = requests.Session()
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {
                pool.submit(
                    download_day, session, symbol, day, args.retries, raw_dir
                ): (symbol, day)
                for symbol, day in tasks
            }
            for future in as_completed(futures):
                symbol, day = futures[future]
                key = f"{symbol}:{day}"
                status = future.result()
                if status == "ok":
                    manifest["downloaded"].append(key)
                elif status == "missing":
                    manifest.setdefault("missing", []).append(key)
                else:
                    manifest.setdefault("failed", []).append(key)
                manifest["downloaded"] = sorted(set(manifest["downloaded"]))
                manifest["missing"] = sorted(set(manifest.get("missing", [])))
                manifest["failed"] = sorted(set(manifest.get("failed", [])))
                tmp = manifest_path.with_suffix(".json.tmp")
                tmp.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                tmp.replace(manifest_path)
                if len(manifest["downloaded"]) % 25 == 0:
                    print(
                        f"downloaded {len(manifest['downloaded'])} "
                        f"missing {len(manifest.get('missing', []))}",
                        flush=True,
                    )
    rows: list[dict[str, Any]] = []
    for symbol, scoped in signals.groupby("symbol", sort=True):
        binance_path = args.binance / f"{symbol}.parquet"
        if not binance_path.exists():
            continue
        binance = pd.read_parquet(binance_path, columns=["open_time", "close"])
        binance = binance.sort_values("open_time").reset_index(drop=True)
        binance_times = binance.open_time.to_numpy()
        binance_closes = binance.close.to_numpy()
        for row in scoped.itertuples(index=False):
            day = row.day
            path = raw_dir / f"{row.symbol}{day}.csv.gz"
            if not path.exists():
                continue
            try:
                bybit = aggregate_1m(path)
            except Exception:
                continue
            bybit_times = bybit.bin_ms.to_numpy()
            bybit_closes = bybit.bybit_close.to_numpy()
            for delay in args.delays:
                entry_ms = int(row.available_ms) + delay * 60_000
                bin_pos = int(np.searchsorted(binance_times, entry_ms, side="left"))
                if bin_pos >= len(binance_times):
                    continue
                bin_close = float(binance_closes[bin_pos])
                by_pos = int(np.searchsorted(bybit_times, entry_ms, side="left"))
                if by_pos >= len(bybit_times):
                    continue
                by_close = float(bybit_closes[by_pos])
                if bin_close <= 0 or by_close <= 0:
                    continue
                rows.append(
                    {
                        "symbol": row.symbol,
                        "available_ms": int(row.available_ms),
                        "delay_minutes": delay,
                        "entry_ms": entry_ms,
                        "binance_close": bin_close,
                        "bybit_close": by_close,
                        "premium_pct": (bin_close / by_close - 1.0) * 100.0,
                    }
                )
    result = pd.DataFrame(rows)
    result.to_parquet(args.output / "signal_premiums.parquet", index=False)
    shutil.rmtree(raw_dir, ignore_errors=True)
    print(
        json.dumps(
            {
                "premium_rows": int(len(result)),
                "signals_covered": int(result.drop_duplicates(["symbol", "available_ms"]).shape[0]),
                "output": str(args.output / "signal_premiums.parquet"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
