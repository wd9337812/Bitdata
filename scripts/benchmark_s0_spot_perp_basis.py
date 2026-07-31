from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests


SPOT_URL = "https://api.binance.com/api/v3/klines"
FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
START = pd.Timestamp("2021-01-01", tz="UTC")
END = pd.Timestamp("2026-08-01", tz="UTC")
SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "LINKUSDT",
    "AVAXUSDT",
    "DOTUSDT",
    "LTCUSDT",
    "BCHUSDT",
    "ETCUSDT",
    "TRXUSDT",
)
ENTRY_THRESHOLDS = (0.002, 0.003, 0.004, 0.005, 0.0075, 0.01)
MAX_HOLD_HOURS = 168
BASIS_STOP_WIDENING = 0.01
MIN_SPOT_QUOTE_VOLUME_24H = 20_000_000.0
BASE_PAIR_COST = 0.003
STRESS_PAIR_COST = 0.005


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--roots",
        type=Path,
        nargs="+",
        default=[
            Path("data/research/binance_um_point_in_time_1h_2020_2023"),
            Path("data/research/binance_um_point_in_time_1h_2024_2025"),
            Path("data/research/binance_um_point_in_time_1h"),
        ],
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/research/s0_spot_perp_basis"),
    )
    parser.add_argument("--refresh", action="store_true")
    return parser.parse_args()


def fetch_paginated(
    url: str,
    params: dict[str, object],
    start_key: str,
    time_column: int | str,
    start_ms: int,
    end_ms: int,
    limit: int,
) -> list:
    rows: list = []
    cursor = start_ms
    session = requests.Session()
    while cursor < end_ms:
        query = {**params, start_key: cursor, "endTime": end_ms - 1, "limit": limit}
        response = session.get(url, params=query, timeout=30)
        response.raise_for_status()
        page = response.json()
        if not page:
            break
        rows.extend(page)
        last = page[-1][time_column] if isinstance(time_column, int) else page[-1][time_column]
        next_cursor = int(last) + 1
        if next_cursor <= cursor:
            raise ValueError("Pagination did not advance")
        cursor = next_cursor
        time.sleep(0.04)
    return rows


def fetch_spot(symbol: str) -> pd.DataFrame:
    rows = fetch_paginated(
        SPOT_URL,
        {"symbol": symbol, "interval": "1h"},
        "startTime",
        0,
        int(START.timestamp() * 1000),
        int(END.timestamp() * 1000),
        1000,
    )
    columns = [
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time",
        "quote_volume",
        "trades",
        "taker_base",
        "taker_quote",
        "ignore",
    ]
    frame = pd.DataFrame(rows, columns=columns)
    for column in ("open", "high", "low", "close", "quote_volume"):
        frame[column] = pd.to_numeric(frame[column])
    frame["open_time"] = frame.open_time.astype("int64")
    return frame[["open_time", "open", "high", "low", "close", "quote_volume"]]


def fetch_funding(symbol: str) -> pd.DataFrame:
    rows = fetch_paginated(
        FUNDING_URL,
        {"symbol": symbol},
        "startTime",
        "fundingTime",
        int(START.timestamp() * 1000),
        int(END.timestamp() * 1000),
        1000,
    )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=["funding_time", "funding_rate"])
    return pd.DataFrame(
        {
            "funding_time": pd.to_numeric(frame.fundingTime).astype("int64"),
            "funding_rate": pd.to_numeric(frame.fundingRate),
        }
    ).drop_duplicates("funding_time")


def load_perp(roots: list[Path], symbol: str) -> pd.DataFrame:
    columns = ["open_time", "open", "high", "low", "close", "quote_volume"]
    parts = []
    for root in roots:
        path = root / "parquet" / f"{symbol}.parquet"
        if path.exists():
            parts.append(pd.read_parquet(path, columns=columns))
    if not parts:
        return pd.DataFrame(columns=columns)
    return (
        pd.concat(parts, ignore_index=True)
        .sort_values("open_time")
        .drop_duplicates("open_time", keep="last")
    )


def load_market(
    roots: list[Path], cache: Path, symbol: str, refresh: bool
) -> tuple[pd.DataFrame, pd.DataFrame]:
    cache.mkdir(parents=True, exist_ok=True)
    spot_path = cache / f"{symbol}-spot.parquet"
    funding_path = cache / f"{symbol}-funding.parquet"
    if refresh or not spot_path.exists():
        fetch_spot(symbol).to_parquet(spot_path, index=False)
    if refresh or not funding_path.exists():
        fetch_funding(symbol).to_parquet(funding_path, index=False)
    spot = pd.read_parquet(spot_path)
    funding = pd.read_parquet(funding_path)
    perp = load_perp(roots, symbol)
    market = perp.merge(spot, on="open_time", suffixes=("_perp", "_spot"), how="inner")
    market["basis_close"] = market.close_perp / market.close_spot - 1.0
    market["spot_liquidity_24h"] = market.quote_volume_spot.rolling(24).sum()
    market["latest_funding"] = pd.merge_asof(
        market[["open_time"]].sort_values("open_time"),
        funding.rename(columns={"funding_time": "open_time"}).sort_values("open_time"),
        on="open_time",
        direction="backward",
    ).funding_rate.to_numpy()
    return market.reset_index(drop=True), funding


def funding_pnl(
    funding: pd.DataFrame, entry_time: int, exit_time: int
) -> float:
    # A short perpetual receives positive funding and pays negative funding.
    return float(
        funding.loc[
            funding.funding_time.gt(entry_time)
            & funding.funding_time.le(exit_time),
            "funding_rate",
        ].sum()
    )


def candidate_entries(markets: dict[str, pd.DataFrame], threshold: float) -> pd.DataFrame:
    rows = []
    for symbol, market in markets.items():
        eligible = market.loc[
            market.basis_close.ge(threshold)
            & market.latest_funding.gt(0)
            & market.spot_liquidity_24h.ge(MIN_SPOT_QUOTE_VOLUME_24H)
        ]
        rows.append(
            eligible[["open_time", "basis_close", "latest_funding"]].assign(symbol=symbol)
        )
    return (
        pd.concat(rows, ignore_index=True)
        .sort_values(
            ["open_time", "basis_close", "latest_funding"],
            ascending=[True, False, False],
        )
        .drop_duplicates("open_time")
        .sort_values("open_time")
    )


def execute_trade(
    market: pd.DataFrame,
    funding: pd.DataFrame,
    signal_time: int,
    entry_basis: float,
) -> dict[str, object] | None:
    entry_time = signal_time + 3_600_000
    entry_rows = market.index[market.open_time.eq(entry_time)]
    if len(entry_rows) != 1:
        return None
    entry_index = int(entry_rows[0])
    entry = market.loc[entry_index]
    exit_index = None
    exit_reason = "time"
    end_index = min(entry_index + MAX_HOLD_HOURS, len(market) - 1)
    for index in range(entry_index + 1, end_index + 1):
        previous = market.loc[index - 1]
        if int(market.loc[index, "open_time"]) != int(previous.open_time) + 3_600_000:
            return None
        if previous.basis_close <= 0:
            exit_index = index
            exit_reason = "converged"
            break
        if previous.basis_close >= entry_basis + BASIS_STOP_WIDENING:
            exit_index = index
            exit_reason = "basis_stop"
            break
    if exit_index is None:
        if end_index - entry_index < MAX_HOLD_HOURS:
            return None
        exit_index = end_index
    exit_row = market.loc[exit_index]
    spot_return = float(exit_row.open_spot / entry.open_spot - 1.0)
    short_perp_return = float(1.0 - exit_row.open_perp / entry.open_perp)
    funding_return = funding_pnl(funding, entry_time, int(exit_row.open_time))
    return {
        "entry_time": entry_time,
        "exit_time": int(exit_row.open_time),
        "hold_hours": int(exit_index - entry_index),
        "entry_basis": float(entry_basis),
        "exit_basis": float(exit_row.open_perp / exit_row.open_spot - 1.0),
        "spot_return": spot_return,
        "short_perp_return": short_perp_return,
        "funding_return": funding_return,
        "pair_gross_return": spot_return + short_perp_return + funding_return,
        "exit_reason": exit_reason,
    }


def simulate(
    markets: dict[str, pd.DataFrame],
    fundings: dict[str, pd.DataFrame],
    threshold: float,
) -> pd.DataFrame:
    candidates = candidate_entries(markets, threshold)
    rows = []
    free_at = -1
    for candidate in candidates.itertuples(index=False):
        entry_time = int(candidate.open_time) + 3_600_000
        if entry_time < free_at:
            continue
        result = execute_trade(
            markets[candidate.symbol],
            fundings[candidate.symbol],
            int(candidate.open_time),
            float(candidate.basis_close),
        )
        if result is None:
            continue
        rows.append({"symbol": candidate.symbol, "signal_time": int(candidate.open_time), **result})
        free_at = int(result["exit_time"])
    return pd.DataFrame(rows)


def metrics(values: pd.Series) -> dict[str, float | int]:
    values = values.dropna().astype(float)
    gains = float(values.loc[values.gt(0)].sum())
    losses = float(-values.loc[values.lt(0)].sum())
    equity = values.div(2.0).add(1.0).cumprod()
    drawdown = equity.div(equity.cummax()).sub(1.0)
    return {
        "trades": int(len(values)),
        "win_rate": float(values.gt(0).mean()) if len(values) else 0.0,
        "profit_factor": gains / losses if losses else (999.0 if gains else 0.0),
        "pair_net_return": float(values.sum()),
        "total_capital_net_return": float(values.sum() / 2.0),
        "mean_total_capital_return": float(values.mean() / 2.0) if len(values) else 0.0,
        "max_drawdown": float(drawdown.min()) if len(drawdown) else 0.0,
    }


def report_scope(trades: pd.DataFrame, start: str, end: str) -> dict[str, object]:
    start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    end_ms = int(pd.Timestamp(end, tz="UTC").timestamp() * 1000)
    scoped = trades.loc[trades.entry_time.ge(start_ms) & trades.entry_time.lt(end_ms)]
    return {
        "base": metrics(scoped.pair_gross_return - BASE_PAIR_COST),
        "stress": metrics(scoped.pair_gross_return - STRESS_PAIR_COST),
    }


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    cache = args.output / "cache"
    markets = {}
    fundings = {}
    for symbol in SYMBOLS:
        market, funding = load_market(args.roots, cache, symbol, args.refresh)
        if not market.empty:
            markets[symbol] = market
            fundings[symbol] = funding
        print(f"loaded {symbol}: {len(market)} hourly rows")

    simulations = {threshold: simulate(markets, fundings, threshold) for threshold in ENTRY_THRESHOLDS}
    validation = {
        str(threshold): report_scope(trades, "2022-01-01", "2023-01-01")
        for threshold, trades in simulations.items()
    }
    qualified = [
        threshold
        for threshold in ENTRY_THRESHOLDS
        if validation[str(threshold)]["stress"]["trades"] >= 10
        and validation[str(threshold)]["stress"]["profit_factor"] >= 1.10
        and validation[str(threshold)]["stress"]["pair_net_return"] > 0
    ]
    frozen = (
        max(
            qualified,
            key=lambda value: (
                validation[str(value)]["stress"]["profit_factor"],
                validation[str(value)]["stress"]["pair_net_return"],
            ),
        )
        if qualified
        else None
    )
    annual = {}
    frozen_trades = simulations[frozen] if frozen is not None else pd.DataFrame()
    if frozen is not None:
        for year in range(2023, 2027):
            annual[str(year)] = report_scope(
                frozen_trades, f"{year}-01-01", f"{year + 1}-01-01"
            )
    accepted = bool(
        frozen is not None
        and all(item["stress"]["trades"] >= 5 for item in annual.values())
        and all(item["stress"]["profit_factor"] >= 1.10 for item in annual.values())
        and all(item["stress"]["pair_net_return"] > 0 for item in annual.values())
    )
    report = {
        "experiment": "s0_spot_perp_basis",
        "method": "Long Binance spot and short the matching USD-M perpetual when the close basis and settled funding are positive; enter and exit on the next hourly open.",
        "symbols": list(markets),
        "costs": {"base_pair": BASE_PAIR_COST, "stress_pair": STRESS_PAIR_COST},
        "validation_2022": validation,
        "frozen_entry_basis": frozen,
        "annual_out_of_sample": annual,
        "accepted": accepted,
        "live_qualified": False,
        "decision": "capital_and_forward_review_required" if accepted else "rejected_not_stable_positive_expectancy",
    }
    if len(frozen_trades):
        frozen_trades.to_parquet(args.output / "frozen_trades.parquet", index=False)
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
