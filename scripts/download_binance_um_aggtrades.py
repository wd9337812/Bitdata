from __future__ import annotations

import argparse
import json
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://data.binance.vision/data/futures/um"
DEFAULT_SOURCE = ROOT / "data" / "research" / "s0_public_1m" / "s0_candidates_1m.parquet"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "binance_um_aggtrades"
RAW_COLUMNS = (
    "agg_trade_id",
    "price",
    "quantity",
    "first_trade_id",
    "last_trade_id",
    "transact_time",
    "is_buyer_maker",
)


@dataclass(frozen=True)
class Task:
    symbol: str
    period: str
    url: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aggregate official Binance USD-M aggTrades into no-lookahead flow features."
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--symbol-workers", type=int, default=2)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--symbols", nargs="*", default=None)
    return parser.parse_args()


def archive_tasks(symbol: str, start_ms: int, end_ms: int) -> list[Task]:
    start = pd.to_datetime(start_ms, unit="ms", utc=True).floor("D")
    end = pd.to_datetime(end_ms, unit="ms", utc=True).floor("D")
    tasks: list[Task] = []
    cursor = start
    while cursor <= end:
        month_start = cursor.replace(day=1)
        month_end = month_start + pd.offsets.MonthEnd(0)
        if cursor == month_start and month_end <= end:
            period = cursor.strftime("%Y-%m")
            filename = f"{symbol}-aggTrades-{period}.zip"
            url = f"{BASE_URL}/monthly/aggTrades/{symbol}/{filename}"
            tasks.append(Task(symbol, period, url))
            cursor = month_end + pd.Timedelta(days=1)
        else:
            period = cursor.strftime("%Y-%m-%d")
            filename = f"{symbol}-aggTrades-{period}.zip"
            url = f"{BASE_URL}/daily/aggTrades/{symbol}/{filename}"
            tasks.append(Task(symbol, period, url))
            cursor += pd.Timedelta(days=1)
    return tasks


def _boolean(series: pd.Series) -> pd.Series:
    if pd.api.types.is_bool_dtype(series):
        return series.fillna(False)
    return (
        series.astype(str)
        .str.strip()
        .str.lower()
        .map({"true": True, "false": False, "1": True, "0": False})
        .fillna(False)
    )


def _read_chunks(path: Path, chunk_size: int = 750_000) -> Iterable[pd.DataFrame]:
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"Expected one aggTrades CSV, got {names!r}")
        with archive.open(names[0]) as handle:
            first_line = handle.readline().decode("utf-8", "replace").strip()
            handle.seek(0)
            has_header = any(character.isalpha() for character in first_line.split(",")[0])
            reader = pd.read_csv(
                handle,
                header=0 if has_header else None,
                names=None if has_header else list(RAW_COLUMNS),
                chunksize=chunk_size,
            )
            for chunk in reader:
                if has_header:
                    chunk.columns = [
                        str(column).strip().lower().replace(" ", "_")
                        for column in chunk.columns
                    ]
                    aliases = {
                        "agg_tradeid": "agg_trade_id",
                        "qty": "quantity",
                        "timestamp": "transact_time",
                        "was_the_buyer_the_maker": "is_buyer_maker",
                    }
                    chunk = chunk.rename(columns=aliases)
                missing = set(RAW_COLUMNS) - set(chunk.columns)
                if missing:
                    raise ValueError(f"Missing aggTrades columns: {sorted(missing)}")
                yield chunk.loc[:, RAW_COLUMNS]


def _aggregate_chunk(frame: pd.DataFrame) -> pd.DataFrame:
    numeric = frame.copy()
    for column in ("price", "quantity", "transact_time"):
        numeric[column] = pd.to_numeric(numeric[column], errors="coerce")
    numeric = numeric.dropna(subset=["price", "quantity", "transact_time"])
    if numeric.empty:
        return pd.DataFrame()
    numeric["minute_ms"] = (
        numeric["transact_time"].astype("int64").floordiv(60_000).mul(60_000)
    )
    numeric["quote"] = numeric["price"] * numeric["quantity"]
    numeric["quote_sq"] = numeric["quote"] ** 2
    numeric["taker_buy"] = ~_boolean(numeric["is_buyer_maker"])
    numeric["buy_quote"] = numeric["quote"].where(numeric["taker_buy"], 0.0)
    numeric["sell_quote"] = numeric["quote"].where(~numeric["taker_buy"], 0.0)
    numeric["buy_count"] = numeric["taker_buy"].astype("int32")
    numeric["sell_count"] = (~numeric["taker_buy"]).astype("int32")
    numeric["signed_quote"] = numeric["buy_quote"] - numeric["sell_quote"]
    grouped = numeric.groupby("minute_ms", sort=True)
    result = grouped.agg(
        trade_count=("quote", "size"),
        quote_sum=("quote", "sum"),
        quote_sq_sum=("quote_sq", "sum"),
        max_quote=("quote", "max"),
        buy_quote=("buy_quote", "sum"),
        sell_quote=("sell_quote", "sum"),
        buy_count=("buy_count", "sum"),
        sell_count=("sell_count", "sum"),
        signed_quote=("signed_quote", "sum"),
        quantity_sum=("quantity", "sum"),
        price_first=("price", "first"),
        price_last=("price", "last"),
        price_high=("price", "max"),
        price_low=("price", "min"),
    )
    result["price_quantity_sum"] = result["quote_sum"]
    return result.reset_index()


def combine_partials(parts: list[pd.DataFrame]) -> pd.DataFrame:
    parts = [part for part in parts if not part.empty]
    if not parts:
        return pd.DataFrame()
    frame = pd.concat(parts, ignore_index=True).sort_values("minute_ms")
    grouped = frame.groupby("minute_ms", sort=True)
    sums = [
        "trade_count",
        "quote_sum",
        "quote_sq_sum",
        "buy_quote",
        "sell_quote",
        "buy_count",
        "sell_count",
        "signed_quote",
        "quantity_sum",
        "price_quantity_sum",
    ]
    result = grouped[sums].sum()
    result["max_quote"] = grouped.max_quote.max()
    result["price_first"] = grouped.price_first.first()
    result["price_last"] = grouped.price_last.last()
    result["price_high"] = grouped.price_high.max()
    result["price_low"] = grouped.price_low.min()
    return result.reset_index()


def finalize_features(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    result = frame.sort_values("minute_ms").copy()
    total = result.quote_sum.replace(0, np.nan)
    counts = result.trade_count.replace(0, np.nan)
    result["agg_trade_count_1m"] = result.trade_count
    result["agg_quote_log_1m"] = np.log1p(result.quote_sum.clip(lower=0))
    result["agg_taker_imbalance_1m"] = result.signed_quote / total
    result["agg_signed_count_ratio_1m"] = (
        result.buy_count - result.sell_count
    ) / counts
    result["agg_mean_quote_1m"] = result.quote_sum / counts
    result["agg_max_share_1m"] = result.max_quote / total
    result["agg_size_hhi_1m"] = result.quote_sq_sum / (total**2)
    result["agg_price_range_1m"] = (
        result.price_high - result.price_low
    ) / result.price_first.replace(0, np.nan)
    result["agg_price_return_1m"] = (
        result.price_last / result.price_first.replace(0, np.nan) - 1.0
    )
    timestamp = pd.to_datetime(result.minute_ms, unit="ms", utc=True)
    indexed = result.set_index(timestamp)
    for minutes in (5, 15):
        rolling = indexed.rolling(f"{minutes}min", min_periods=max(2, minutes // 2))
        quote = rolling.quote_sum.sum()
        signed = rolling.signed_quote.sum()
        count = rolling.trade_count.sum()
        result[f"agg_trade_count_{minutes}m"] = count.to_numpy()
        result[f"agg_quote_log_{minutes}m"] = np.log1p(quote.clip(lower=0)).to_numpy()
        result[f"agg_taker_imbalance_{minutes}m"] = (signed / quote.replace(0, np.nan)).to_numpy()
        result[f"agg_mean_quote_{minutes}m"] = (quote / count.replace(0, np.nan)).to_numpy()
        result[f"agg_flow_persistence_{minutes}m"] = (
            np.sign(indexed.agg_taker_imbalance_1m)
            .rolling(f"{minutes}min", min_periods=max(2, minutes // 2))
            .mean()
            .to_numpy()
        )
    result.insert(0, "available_ms", result.minute_ms.astype("int64") + 60_000)
    keep = ["available_ms", *[column for column in result if column.startswith("agg_")]]
    return result.loc[:, keep].replace([np.inf, -np.inf], np.nan)


def aggregate_archive(path: Path) -> pd.DataFrame:
    return finalize_features(
        combine_partials([_aggregate_chunk(chunk) for chunk in _read_chunks(path)])
    )


def _download(task: Task, temp_dir: Path, retries: int) -> tuple[pd.DataFrame | None, str | None]:
    error: str | None = None
    for attempt in range(max(1, retries)):
        temp_path: Path | None = None
        try:
            with requests.get(task.url, timeout=120, stream=True) as response:
                if response.status_code == 404:
                    return None, None
                response.raise_for_status()
                with tempfile.NamedTemporaryFile(
                    dir=temp_dir,
                    suffix=".zip",
                    delete=False,
                ) as handle:
                    temp_path = Path(handle.name)
                    for block in response.iter_content(chunk_size=1024 * 1024):
                        if block:
                            handle.write(block)
            return aggregate_archive(temp_path), None
        except Exception as exc:
            error = str(exc)
            if attempt + 1 < retries:
                time.sleep(0.75 * (2**attempt))
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
    return None, error


def _process_symbol(
    symbol: str,
    start_ms: int,
    end_ms: int,
    output: Path,
    retries: int,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    target = output / f"{symbol}-aggtrades.parquet"
    if target.exists() and target.stat().st_size:
        cached = pd.read_parquet(target, columns=["available_ms"])
        return {
            "symbol": symbol,
            "rows": int(len(cached)),
            "start_ms": int(cached.available_ms.min()),
            "end_ms": int(cached.available_ms.max()),
            "cached": True,
        }, []
    tasks = archive_tasks(symbol, start_ms, end_ms)
    parts: list[pd.DataFrame] = []
    errors: list[dict[str, str]] = []
    temp_dir = output / ".tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    for task in tasks:
        frame, error = _download(task, temp_dir, retries)
        if frame is not None and not frame.empty:
            parts.append(frame)
        if error:
            errors.append({"symbol": symbol, "period": task.period, "error": error})
    if parts:
        combined = (
            pd.concat(parts, ignore_index=True)
            .drop_duplicates("available_ms", keep="last")
            .sort_values("available_ms")
        )
        combined.insert(1, "symbol", symbol)
        combined.to_parquet(target, index=False)
    else:
        combined = pd.DataFrame(columns=["available_ms", "symbol"])
    return {
        "symbol": symbol,
        "rows": int(len(combined)),
        "start_ms": int(combined.available_ms.min()) if len(combined) else None,
        "end_ms": int(combined.available_ms.max()) if len(combined) else None,
        "cached": False,
    }, errors


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    source = pd.read_parquet(args.source, columns=["symbol", "open_time"])
    if args.symbols:
        wanted = {symbol.upper() for symbol in args.symbols}
        source = source[source.symbol.isin(wanted)]
    ranges = source.groupby("symbol").open_time.agg(["min", "max"])
    coverage: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.symbol_workers)) as pool:
        futures = {
            pool.submit(
                _process_symbol,
                symbol,
                int(row["min"]),
                int(row["max"]),
                args.output,
                args.retries,
            ): symbol
            for symbol, row in ranges.iterrows()
        }
        for future in as_completed(futures):
            item, failures = future.result()
            coverage.append(item)
            errors.extend(failures)
            print(
                f"{item['symbol']}: {item['rows']:,} rows"
                f"{' (cached)' if item['cached'] else ''}",
                flush=True,
            )
    manifest = {
        "source": str(args.source),
        "symbols": len(coverage),
        "rows": int(sum(item["rows"] for item in coverage)),
        "coverage": sorted(coverage, key=lambda item: item["symbol"]),
        "errors": errors,
    }
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
