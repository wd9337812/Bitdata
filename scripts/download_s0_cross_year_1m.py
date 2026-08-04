from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data" / "research" / "binance_um_1m_cross_year"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_binance_um_point_in_time_1h import (  # noqa: E402
    ARCHIVE_BASE,
    month_range,
    parse_kline_archive,
    request_with_retry,
    verify_checksum,
)
from scripts.download_s0_cross_year_metrics import (  # noqa: E402
    DEFAULT_FUNDING,
    top_symbols,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download checksum-verified Binance USD-M monthly 1m kline archives "
            "across years, convert each symbol to one compact parquet, and delete "
            "the staging zips so disk usage stays bounded."
        )
    )
    parser.add_argument("--funding", type=Path, default=DEFAULT_FUNDING)
    parser.add_argument(
        "--universe",
        type=Path,
        default=ROOT / "data" / "research" / "s0_public_1m" / "universe.json",
        help="Liquid-symbol manifest (quote-volume sorted); preferred over funding counts.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--top", type=int, default=100)
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--start", default="2020-01")
    parser.add_argument("--end", default="2026-07")
    parser.add_argument("--workers", type=int, default=24)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--no-checksum", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def archive_url(symbol: str, month: str) -> str:
    return (
        f"{ARCHIVE_BASE}/data/futures/um/monthly/klines/"
        f"{symbol}/1m/{symbol}-1m-{month}.zip"
    )


def download_month(
    session: requests.Session,
    symbol: str,
    month: str,
    retries: int,
    checksum: bool,
    output: Path,
) -> tuple[str, str]:
    """Return (month, 'ok' | 'missing' | 'failed')."""
    url = archive_url(symbol, month)
    try:
        response = request_with_retry(session, url, retries=retries)
        if checksum:
            verify_checksum(session, url, response.content, retries)
        target = output / "raw" / symbol / f"{symbol}-1m-{month}.zip"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(response.content)
        return month, "ok"
    except requests.exceptions.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return month, "missing"
        return month, "failed"
    except Exception:
        return month, "failed"


def build_symbol_parquet(
    output: Path,
    symbol: str,
    months: list[str],
    raw_dir: Path,
) -> tuple[int, list[str]]:
    parts: list[pd.DataFrame] = []
    parsed: list[str] = []
    for month in months:
        path = raw_dir / f"{symbol}-1m-{month}.zip"
        if not path.exists():
            continue
        payload = path.read_bytes()
        parts.append(parse_kline_archive(payload, symbol))
        parsed.append(month)
    if not parts:
        return 0, []
    frame = (
        pd.concat(parts, ignore_index=True)
        .drop_duplicates("open_time", keep="last")
        .sort_values("open_time")
        .reset_index(drop=True)
    )
    keep = [
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "count",
        "taker_buy_volume",
        "taker_buy_quote_volume",
    ]
    frame = frame[keep]
    for column in ("open", "high", "low", "close"):
        frame[column] = frame[column].astype("float32")
    for column in ("volume", "quote_volume", "taker_buy_volume", "taker_buy_quote_volume"):
        frame[column] = frame[column].astype("float64")
    frame["count"] = frame["count"].astype("int32")
    target = output / "parquet" / f"{symbol}.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(target, index=False)
    return int(len(frame)), parsed


def main() -> None:
    args = parse_args()
    if args.symbols:
        symbols = [symbol.upper() for symbol in args.symbols]
    elif args.universe.exists():
        manifest = json.loads(args.universe.read_text(encoding="utf-8"))
        symbols = [
            str(item["symbol"]).upper()
            for item in manifest.get("symbols", [])
        ][: args.top]
        print(
            f"universe: {args.universe} (quote-volume sorted, top {len(symbols)})",
            flush=True,
        )
    else:
        symbols = top_symbols(args.funding, args.top)
    months = month_range(args.start, args.end)
    args.output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.exists()
        else {"completed": {}, "summary": {}}
    )
    completed = manifest.get("completed", {})
    if args.force:
        completed = {}
    todo = [symbol for symbol in symbols if symbol not in completed]
    print(
        json.dumps(
            {
                "symbols": len(symbols),
                "months": months,
                "todo": len(todo),
                "output": str(args.output),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    session = requests.Session()
    for symbol in todo:
        raw_dir = args.output / "raw" / symbol
        raw_dir.mkdir(parents=True, exist_ok=True)
        results: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {
                pool.submit(
                    download_month,
                    session,
                    symbol,
                    month,
                    args.retries,
                    not args.no_checksum,
                    args.output,
                ): month
                for month in months
            }
            for future in as_completed(futures):
                month, status = future.result()
                results[month] = status
        ok_months = sorted(month for month, status in results.items() if status == "ok")
        missing = sorted(month for month, status in results.items() if status == "missing")
        failed = sorted(month for month, status in results.items() if status == "failed")
        rows = 0
        if ok_months:
            try:
                rows, parsed = build_symbol_parquet(
                    args.output, symbol, ok_months, raw_dir
                )
                if parsed != ok_months:
                    raise RuntimeError(
                        f"{symbol}: parquet parsed {len(parsed)}/{len(ok_months)} archives"
                    )
                shutil.rmtree(raw_dir, ignore_errors=True)
            except Exception as exc:
                print(
                    json.dumps(
                        {
                            "symbol": symbol,
                            "error": str(exc),
                            "kept_raw": True,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                continue
        entry: dict[str, Any] = {
            "rows": rows,
            "downloaded_months": len(ok_months),
            "missing_months": missing,
            "failed_months": failed,
        }
        if rows:
            target = args.output / "parquet" / f"{symbol}.parquet"
            existing = pd.read_parquet(target, columns=["open_time"])
            entry["first_open_time"] = int(existing.open_time.min())
            entry["last_open_time"] = int(existing.open_time.max())
        if failed:
            entry["status"] = "partial"
            manifest.setdefault("partial", {})[symbol] = entry
            print(
                f"partial {symbol}: rows={rows} ok={len(ok_months)} "
                f"missing={len(missing)} failed={failed} -> retry next run",
                flush=True,
            )
        else:
            completed[symbol] = entry
            print(
                f"done {symbol}: rows={rows} ok={len(ok_months)} "
                f"missing={len(missing)} failed={len(failed)} "
                f"completed={len(completed)}/{len(symbols)}",
                flush=True,
            )
        manifest["completed"] = completed
        manifest["summary"] = {
            "source": "https://data.binance.vision USD-M monthly 1m klines",
            "checksum_verified": not args.no_checksum,
            "completed_symbols": len(completed),
        }
        tmp_manifest = manifest_path.with_suffix(".json.tmp")
        tmp_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_manifest.replace(manifest_path)
    total_rows = sum(int(item["rows"]) for item in completed.values())
    print(
        json.dumps(
            {
                "completed_symbols": len(completed),
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
