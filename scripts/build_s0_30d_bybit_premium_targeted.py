from __future__ import annotations

import argparse
import json
import sys
import tempfile
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

from scripts.build_s0_30d_bybit_premium import aggregate_1m  # noqa: E402

DEFAULT_SIGNALS = (
    ROOT / "data" / "research" / "s0_30d_momentum_adaptive_v2"
    / "signals_all.parquet"
)
DEFAULT_PREMIUM = (
    ROOT / "data" / "research" / "s0_30d_bybit_premium"
    / "signal_premiums.parquet"
)
DEFAULT_BINANCE = ROOT / "data" / "research" / "binance_um_30d_all_event_1m" / "parquet"
BASE_URL = "https://public.bybit.com/trading"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Parallel targeted Binance-Bybit premium build for 30d signals; "
            "downloads Bybit daily files, aggregates 1m, deletes files, saves "
            "incrementally."
        )
    )
    parser.add_argument("--signals", type=Path, default=DEFAULT_SIGNALS)
    parser.add_argument("--premium", type=Path, default=DEFAULT_PREMIUM)
    parser.add_argument("--binance", type=Path, default=DEFAULT_BINANCE)
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--delays", type=int, nargs="+", default=[0, 5, 15])
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--retries", type=int, default=4)
    return parser.parse_args()


def download_to(
    session: requests.Session,
    symbol: str,
    day: str,
    target: Path,
    retries: int,
) -> bool:
    url = f"{BASE_URL}/{symbol}/{symbol}{day}.csv.gz"
    for attempt in range(max(1, retries)):
        try:
            response = session.get(url, timeout=180, stream=True)
            if response.status_code == 404:
                return False
            response.raise_for_status()
            with target.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    if chunk:
                        handle.write(chunk)
            return True
        except Exception:
            if attempt + 1 < retries:
                time.sleep(min(2**attempt, 8))
    return False


def main() -> None:
    args = parse_args()
    signals = pd.read_parquet(args.signals)
    signals["day"] = pd.to_datetime(
        signals.available_ms, unit="ms", utc=True
    ).dt.strftime("%Y-%m-%d")
    symbols = args.symbols or sorted(signals.symbol.unique())
    existing = (
        pd.read_parquet(args.premium)
        if args.premium.exists()
        else pd.DataFrame()
    )
    existing_keys = (
        set(
            existing.symbol
            + ":"
            + existing.available_ms.astype(str)
            + ":"
            + existing.delay_minutes.astype(str)
        )
        if not existing.empty
        else set()
    )
    tasks: list[tuple[str, str, Path]] = []
    needed: dict[str, list[dict[str, Any]]] = {}
    with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as tmp_dir:
        tmp = Path(tmp_dir)
        for symbol in symbols:
            binance_path = args.binance / f"{symbol}.parquet"
            if not binance_path.exists():
                print(f"skip {symbol}: no binance parquet", flush=True)
                continue
            scoped = signals[signals.symbol.eq(symbol)]
            for row in scoped.itertuples(index=False):
                missing_delays = [
                    delay
                    for delay in args.delays
                    if f"{symbol}:{int(row.available_ms)}:{delay}"
                    not in existing_keys
                ]
                if not missing_delays:
                    continue
                target = tmp / f"{symbol}{row.day}.csv.gz"
                tasks.append((symbol, row.day, target))
                needed.setdefault(symbol, []).append(
                    {
                        "available_ms": int(row.available_ms),
                        "day": row.day,
                        "delays": missing_delays,
                        "path": target,
                    }
                )
        print(json.dumps({"tasks": len(tasks), "symbols": len(needed)}), flush=True)
        session = requests.Session()
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {
                pool.submit(
                    download_to, session, symbol, day, target, args.retries
                ): (symbol, day, target)
                for symbol, day, target in tasks
            }
            for future in as_completed(futures):
                symbol, day, target = futures[future]
                if not future.result():
                    target.unlink(missing_ok=True)
        # Aggregate per symbol and save incrementally.
        new_rows: list[dict[str, Any]] = []
        for symbol, items in needed.items():
            binance = pd.read_parquet(
                args.binance / f"{symbol}.parquet", columns=["open_time", "close"]
            ).sort_values("open_time").reset_index(drop=True)
            binance_times = binance.open_time.to_numpy()
            binance_closes = binance.close.to_numpy()
            added = 0
            for item in items:
                target = item["path"]
                if not target.exists():
                    continue
                try:
                    bybit = aggregate_1m(target)
                except Exception:
                    target.unlink(missing_ok=True)
                    continue
                target.unlink(missing_ok=True)
                bybit_times = bybit.bin_ms.to_numpy()
                bybit_closes = bybit.bybit_close.to_numpy()
                for delay in item["delays"]:
                    entry_ms = item["available_ms"] + delay * 60_000
                    bin_pos = int(
                        np.searchsorted(binance_times, entry_ms, side="left")
                    )
                    if bin_pos >= len(binance_times):
                        continue
                    by_pos = int(
                        np.searchsorted(bybit_times, entry_ms, side="left")
                    )
                    if by_pos >= len(bybit_times):
                        continue
                    bin_close = float(binance_closes[bin_pos])
                    by_close = float(bybit_closes[by_pos])
                    if bin_close <= 0 or by_close <= 0:
                        continue
                    new_rows.append(
                        {
                            "symbol": symbol,
                            "available_ms": item["available_ms"],
                            "delay_minutes": delay,
                            "entry_ms": entry_ms,
                            "binance_close": bin_close,
                            "bybit_close": by_close,
                            "premium_pct": (bin_close / by_close - 1.0) * 100.0,
                        }
                    )
                    added += 1
            combined = pd.concat(
                [existing, pd.DataFrame(new_rows)], ignore_index=True
            ).drop_duplicates(
                ["symbol", "available_ms", "delay_minutes"], keep="last"
            ).sort_values(["symbol", "available_ms", "delay_minutes"])
            args.premium.parent.mkdir(parents=True, exist_ok=True)
            combined.to_parquet(args.premium, index=False)
            print(f"done {symbol}: added={added}", flush=True)
        print(
            json.dumps(
                {
                    "new_rows": len(new_rows),
                    "total_rows": int(len(combined)),
                    "output": str(args.premium),
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
