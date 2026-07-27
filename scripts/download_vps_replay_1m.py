from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HISTORY = (
    ROOT
    / "data"
    / "research"
    / "vps_history_20260727"
    / "shadow_trades_compact.csv.gz"
)
DEFAULT_OUTPUT = ROOT / "data" / "research" / "vps_replay_1m_20260727"
BASE_URL = "https://fapi.binance.com"
INTERVAL_MS = 60_000
KLINE_COLUMNS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trades",
    "taker_buy_base",
    "taker_buy_quote",
    "ignore",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download Binance 1m klines covering VPS shadow opportunities."
    )
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--versions",
        default="v4.10,v4.11,v4.7.2,v4.7.3,v4.7.4",
        help="Comma-separated V4 versions used to derive the replay universe.",
    )
    parser.add_argument("--request-pause", type=float, default=0.18)
    return parser.parse_args()


def _request(
    session: requests.Session,
    symbol: str,
    start_time: int,
    end_time: int,
) -> list[list[Any]]:
    params = {
        "symbol": symbol,
        "interval": "1m",
        "startTime": start_time,
        "endTime": end_time,
        "limit": 1_000,
    }
    for attempt in range(5):
        response = session.get(
            f"{BASE_URL}/fapi/v1/klines",
            params=params,
            timeout=30,
        )
        if response.status_code == 200:
            return response.json()
        if response.status_code in {418, 429}:
            retry_after = float(response.headers.get("Retry-After") or 5)
            time.sleep(max(retry_after, 2.0 * (attempt + 1)))
            continue
        if response.status_code == 400 and "Invalid symbol" in response.text:
            return []
        if attempt == 4:
            response.raise_for_status()
        time.sleep(1.5 * (attempt + 1))
    return []


def download_symbol(
    session: requests.Session,
    symbol: str,
    start_time: int,
    end_time: int,
    output: Path,
    pause: float,
) -> dict[str, Any]:
    path = output / "parquet" / f"{symbol}.parquet"
    if path.exists():
        frame = pd.read_parquet(path, columns=["open_time"])
        if (
            not frame.empty
            and int(frame.open_time.min()) <= start_time
            and int(frame.open_time.max()) >= end_time - INTERVAL_MS
        ):
            return {"symbol": symbol, "rows": len(frame), "status": "cached"}

    rows: list[list[Any]] = []
    cursor = start_time
    requests_used = 0
    while cursor <= end_time:
        batch = _request(session, symbol, cursor, end_time)
        requests_used += 1
        time.sleep(max(pause, 0.05))
        if not batch:
            break
        rows.extend(batch)
        last_open = int(batch[-1][0])
        if last_open < cursor or len(batch) < 1_000:
            break
        cursor = last_open + INTERVAL_MS

    if not rows:
        return {
            "symbol": symbol,
            "rows": 0,
            "requests": requests_used,
            "status": "unavailable",
        }
    frame = pd.DataFrame(rows, columns=KLINE_COLUMNS)
    for column in KLINE_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = (
        frame.dropna(subset=["open_time", "open", "high", "low", "close"])
        .drop_duplicates("open_time", keep="last")
        .sort_values("open_time")
    )
    frame["open_time"] = frame.open_time.astype("int64")
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False, compression="zstd")
    return {
        "symbol": symbol,
        "rows": len(frame),
        "requests": requests_used,
        "status": "downloaded",
    }


def main() -> None:
    args = parse_args()
    versions = {
        value.strip()
        for value in str(args.versions).split(",")
        if value.strip()
    }
    source = pd.read_csv(
        args.history,
        usecols=[
            "opened_at",
            "closed_at",
            "symbol",
            "strategy_family",
            "strategy_version",
        ],
        low_memory=False,
    )
    source = source[
        source.strategy_family.eq("extreme_v4_roll")
        & source.strategy_version.isin(versions)
    ].copy()
    if source.empty:
        raise SystemExit("No matching V4 shadow rows were found.")
    opened = pd.to_datetime(source.opened_at, utc=True)
    closed = pd.to_datetime(source.closed_at, utc=True)
    start_time = int((opened.min() - pd.Timedelta(minutes=15)).timestamp() * 1_000)
    end_time = int(
        (max(opened.max(), closed.max()) + pd.Timedelta(minutes=15)).timestamp()
        * 1_000
    )
    symbols = sorted(source.symbol.dropna().astype(str).unique())
    args.output.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = "Bitdata-VPS-Replay/1.0"
    results: list[dict[str, Any]] = []
    for index, symbol in enumerate(symbols, start=1):
        result = download_symbol(
            session,
            symbol,
            start_time,
            end_time,
            args.output,
            args.request_pause,
        )
        results.append(result)
        if index % 10 == 0 or index == len(symbols):
            print(
                f"{index}/{len(symbols)} symbols, "
                f"{sum(item.get('rows', 0) for item in results):,} rows",
                flush=True,
            )
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(args.history),
        "versions": sorted(versions),
        "start_time": start_time,
        "end_time": end_time,
        "symbols": len(symbols),
        "rows": sum(item.get("rows", 0) for item in results),
        "requests": sum(item.get("requests", 0) for item in results),
        "results": results,
    }
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({key: manifest[key] for key in manifest if key != "results"}, indent=2))


if __name__ == "__main__":
    main()
