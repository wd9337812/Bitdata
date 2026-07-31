from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    from scripts import benchmark_s0_daily_order_flow as data_source
except ModuleNotFoundError:  # Direct script execution adds scripts/, not the repo root.
    import benchmark_s0_daily_order_flow as data_source


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_pump_fade"
PRICE_INCREASE = 0.90
VOLUME_MULTIPLE = 5.0
MAX_VOLUME_FRACTION = 0.60
MIN_ROLLING_24H_QUOTE_VOLUME = 20_000_000.0
MIN_AGE_HOURS = 60 * 24
STOP = 0.15
TARGET = 0.30
HOLD_HOURS = 24
BASE_COST = 0.0012
STRESS_COST = 0.0024
EVALUATION_YEARS = (2022, 2023, 2024, 2025, 2026)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit a paper-derived, point-in-time pump-fade event."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def events_for_symbol(hourly: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if hourly.empty:
        return pd.DataFrame()
    frame = hourly.sort_values("open_time").copy()
    frame["prior_open_mean_12h"] = frame.open.shift(1).rolling(12, min_periods=12).mean()
    prior_volume = frame.quote_volume.shift(1)
    frame["prior_volume_ewma_20d"] = prior_volume.ewm(
        span=20 * 24, min_periods=10 * 24, adjust=False
    ).mean()
    frame["prior_volume_max_30d"] = prior_volume.rolling(
        30 * 24, min_periods=20 * 24
    ).max()
    frame["rolling_24h_quote_volume"] = frame.quote_volume.rolling(
        24, min_periods=24
    ).sum()
    frame["price_increase"] = frame.high / frame.prior_open_mean_12h - 1.0
    frame["volume_multiple"] = (
        frame.quote_volume / frame.prior_volume_ewma_20d.replace(0.0, np.nan)
    )
    frame["age_hours"] = np.arange(len(frame))
    selected = frame.loc[
        frame.age_hours.ge(MIN_AGE_HOURS)
        & frame.price_increase.ge(PRICE_INCREASE)
        & frame.volume_multiple.ge(VOLUME_MULTIPLE)
        & frame.quote_volume.ge(MAX_VOLUME_FRACTION * frame.prior_volume_max_30d)
        & frame.rolling_24h_quote_volume.ge(MIN_ROLLING_24H_QUOTE_VOLUME)
    ].copy()
    selected["symbol"] = symbol
    selected["available_ms"] = selected.open_time + 3_600_000
    selected["strength"] = (
        selected.price_increase / PRICE_INCREASE
        + selected.volume_multiple / VOLUME_MULTIPLE
    )
    return selected


def collect_events() -> pd.DataFrame:
    symbols = sorted(
        {
            path.stem
            for root in data_source.DATA_DIRS
            for path in root.glob("*.parquet")
        }
    )
    parts = []
    for symbol in symbols:
        events = events_for_symbol(data_source.load_hourly_symbol(symbol), symbol)
        if not events.empty:
            parts.append(events)
    if not parts:
        return pd.DataFrame()
    return (
        pd.concat(parts, ignore_index=True)
        .sort_values(
            ["available_ms", "strength", "rolling_24h_quote_volume"],
            ascending=[True, False, False],
        )
        .drop_duplicates("available_ms")
        .sort_values("available_ms")
        .reset_index(drop=True)
    )


def execute_short_path(
    entry_index: int,
    hourly: pd.DataFrame,
    stop: float = STOP,
    target: float = TARGET,
    hold_hours: int = HOLD_HOURS,
) -> dict[str, Any]:
    entry = float(hourly.loc[entry_index, "open"])
    stop_price = entry * (1.0 + stop)
    target_price = entry * (1.0 - target)
    exit_index = min(entry_index + hold_hours, len(hourly) - 1)
    for index in range(entry_index, min(entry_index + hold_hours, len(hourly))):
        row = hourly.loc[index]
        if row.open >= stop_price:
            return {
                "exit_index": index,
                "gross_return": 1.0 - float(row.open) / entry,
                "exit_reason": "stop_gap",
            }
        if row.high >= stop_price:
            return {"exit_index": index, "gross_return": -stop, "exit_reason": "stop"}
        if row.open <= target_price:
            return {
                "exit_index": index,
                "gross_return": 1.0 - float(row.open) / entry,
                "exit_reason": "target_gap",
            }
        if row.low <= target_price:
            return {"exit_index": index, "gross_return": target, "exit_reason": "target"}
    exit_price = float(hourly.loc[exit_index, "open"])
    return {
        "exit_index": exit_index,
        "gross_return": 1.0 - exit_price / entry,
        "exit_reason": "time",
    }


def simulate(events: pd.DataFrame) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    cache: dict[str, pd.DataFrame] = {}
    rows = []
    missing = []
    free_at = -1
    for event in events.itertuples(index=False):
        if int(event.available_ms) < free_at:
            continue
        if event.symbol not in cache:
            cache[event.symbol] = data_source.load_hourly_symbol(event.symbol)
        hourly = cache[event.symbol]
        indices = hourly.index[hourly.open_time.eq(event.available_ms)].tolist()
        if not indices:
            missing.append({"symbol": event.symbol, "available_ms": int(event.available_ms)})
            continue
        entry_index = indices[0]
        result = execute_short_path(entry_index, hourly)
        exit_index = int(result.pop("exit_index"))
        exit_ms = int(hourly.loc[exit_index, "open_time"])
        rows.append(
            {
                "symbol": event.symbol,
                "signal_time": pd.to_datetime(event.open_time, unit="ms", utc=True),
                "entry_time": pd.to_datetime(event.available_ms, unit="ms", utc=True),
                "exit_time": pd.to_datetime(exit_ms, unit="ms", utc=True),
                "price_increase": float(event.price_increase),
                "volume_multiple": float(event.volume_multiple),
                **result,
            }
        )
        free_at = exit_ms + 1
    return pd.DataFrame(rows), missing


def remove_top_winners(trades: pd.DataFrame, count: int = 3) -> pd.DataFrame:
    if trades.empty:
        return trades
    return trades.drop(
        index=trades.gross_return.nlargest(min(count, len(trades))).index
    )


def summarize(trades: pd.DataFrame) -> dict[str, Any]:
    annual: dict[str, Any] = {}
    years = trades.entry_time.dt.year if not trades.empty else pd.Series(dtype=int)
    for year in EVALUATION_YEARS:
        scoped = trades.loc[years.eq(year)].copy()
        trimmed = remove_top_winners(scoped)
        annual[str(year)] = {
            "base": data_source.metric(scoped.gross_return - BASE_COST),
            "stress": data_source.metric(scoped.gross_return - STRESS_COST),
            "stress_without_top_three_winners": data_source.metric(
                trimmed.gross_return - STRESS_COST
            ),
        }
    return annual


def run() -> tuple[dict[str, Any], pd.DataFrame]:
    events = collect_events()
    trades, missing = simulate(events)
    annual = summarize(trades)
    passes = all(
        annual[str(year)]["stress"]["trades"] >= 10
        and annual[str(year)]["stress"]["profit_factor"] > 1.0
        and annual[str(year)]["stress"]["net_pct_points"] > 0.0
        and annual[str(year)]["stress_without_top_three_winners"]["profit_factor"] > 1.0
        for year in EVALUATION_YEARS
    )
    report = {
        "experiment": "s0_pump_fade",
        "method": "Paper-derived joint price-volume anomaly: completed-hour high at least 90% above the prior 12-hour open mean, quote volume at least 5x the prior 20-day EWMA and 60% of the prior 30-day maximum; next-hour short entry, 15% stop, 30% target, 24-hour timeout, one non-overlapping S0 position.",
        "source_boundary": "The source paper detects manipulation on Poloniex and does not claim a profitable short strategy. This Binance perpetual fade is a separately tested trading hypothesis.",
        "candidate_events": int(len(events)),
        "selected_trades": int(len(trades)),
        "missing_entries": missing,
        "costs": {"base": BASE_COST, "stress": STRESS_COST},
        "annual": annual,
        "decision": (
            "research_candidate_requires_fresh_forward_validation"
            if passes and not missing
            else "rejected_not_positive_expectancy"
        ),
        "live_qualified": False,
    }
    return report, trades


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report, trades = run()
    trades.to_parquet(args.output / "trades.parquet", index=False)
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
