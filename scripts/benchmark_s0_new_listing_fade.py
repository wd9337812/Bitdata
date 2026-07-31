from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


BASE_ROUND_TRIP_COST = 0.0012
STRESS_ROUND_TRIP_COST = 0.0024
SIGNAL_AGE_HOURS = 72
MAX_HOLD_HOURS = 168
MIN_INITIAL_MOVE = 0.02
MIN_24H_QUOTE_VOLUME = 20_000_000.0
WINDOWS = {
    "development_2020_2022": ("2020-02-01", "2023-01-01"),
    "validation_2023": ("2023-01-01", "2024-01-01"),
    "test_2024": ("2024-01-01", "2025-01-01"),
    "blind_2025": ("2025-01-01", "2026-01-01"),
    "final_2026": ("2026-01-01", "2027-01-01"),
}


@dataclass(frozen=True)
class ExitProfile:
    name: str
    stop: float | None
    take: float | None


PROFILES = (
    ExitProfile("time_only", None, None),
    ExitProfile("symmetric_10pct", 0.10, 0.10),
    ExitProfile("symmetric_15pct", 0.15, 0.15),
    ExitProfile("asymmetric_10_20pct", 0.10, 0.20),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--history",
        type=Path,
        default=Path("data/research/binance_um_point_in_time_1h_2020_2023"),
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=Path("data/research/binance_um_point_in_time_1h_2024_2025"),
    )
    parser.add_argument(
        "--extension",
        type=Path,
        default=Path("data/research/binance_um_point_in_time_1h"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/research/s0_new_listing_fade"),
    )
    return parser.parse_args()


def available_names(roots: list[Path]) -> list[str]:
    return sorted(
        {
            path.name
            for root in roots
            for path in (root / "parquet").glob("*.parquet")
        }
    )


def load_symbol(name: str, roots: list[Path]) -> pd.DataFrame:
    columns = [
        "symbol",
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "quote_volume",
    ]
    parts = []
    for root in roots:
        path = root / "parquet" / name
        if path.exists():
            parts.append(pd.read_parquet(path, columns=columns))
    if not parts:
        return pd.DataFrame(columns=columns)
    return (
        pd.concat(parts, ignore_index=True)
        .sort_values("open_time")
        .drop_duplicates("open_time", keep="last")
        .reset_index(drop=True)
    )


def build_opportunity(hourly: pd.DataFrame) -> dict[str, object] | None:
    if len(hourly) <= SIGNAL_AGE_HOURS + MAX_HOLD_HOURS:
        return None
    first_time = int(hourly.open_time.iloc[0])
    signal_time = first_time + SIGNAL_AGE_HOURS * 3_600_000
    signal_rows = hourly.index[hourly.open_time.ge(signal_time)]
    if len(signal_rows) == 0:
        return None
    entry_index = int(signal_rows[0])
    if entry_index < 24 or entry_index + MAX_HOLD_HOURS >= len(hourly):
        return None
    # Gaps around the listing window make an exact 72-hour event unverifiable.
    observed = hourly.iloc[: entry_index + 1].open_time.diff().dropna()
    if observed.gt(3_600_000).any():
        return None
    initial_move = float(
        hourly.close.iloc[entry_index - 1] / hourly.open.iloc[0] - 1.0
    )
    quote_volume_24h = float(
        hourly.quote_volume.iloc[entry_index - 24 : entry_index].sum()
    )
    if abs(initial_move) < MIN_INITIAL_MOVE:
        return None
    if quote_volume_24h < MIN_24H_QUOTE_VOLUME:
        return None
    return {
        "symbol": str(hourly.symbol.iloc[0]),
        "listing_time": first_time,
        "entry_time": int(hourly.open_time.iloc[entry_index]),
        "entry_index": entry_index,
        "initial_move": initial_move,
        "side": -1 if initial_move > 0 else 1,
        "quote_volume_24h": quote_volume_24h,
    }


def apply_exit(
    hourly: pd.DataFrame,
    opportunity: dict[str, object],
    profile: ExitProfile,
) -> dict[str, object]:
    entry_index = int(opportunity["entry_index"])
    entry = float(hourly.open.iloc[entry_index])
    side = int(opportunity["side"])
    path = hourly.iloc[entry_index : entry_index + MAX_HOLD_HOURS + 1]
    stop_price = None if profile.stop is None else entry * (1.0 - side * profile.stop)
    take_price = None if profile.take is None else entry * (1.0 + side * profile.take)
    exit_price = float(path.close.iloc[-1])
    exit_time = int(path.open_time.iloc[-1])
    exit_reason = "time"
    for row in path.iloc[1:].itertuples(index=False):
        if side > 0:
            stop_hit = stop_price is not None and row.low <= stop_price
            take_hit = take_price is not None and row.high >= take_price
        else:
            stop_hit = stop_price is not None and row.high >= stop_price
            take_hit = take_price is not None and row.low <= take_price
        # A one-hour archive cannot reveal intrabar ordering; stop-first is conservative.
        if stop_hit:
            exit_price = float(stop_price)
            exit_time = int(row.open_time)
            exit_reason = "stop"
            break
        if take_hit:
            exit_price = float(take_price)
            exit_time = int(row.open_time)
            exit_reason = "take"
            break
    gross_return = side * (exit_price / entry - 1.0)
    return {
        **{key: value for key, value in opportunity.items() if key != "entry_index"},
        "profile": profile.name,
        "entry": entry,
        "exit": exit_price,
        "exit_time": exit_time,
        "exit_reason": exit_reason,
        "gross_return": gross_return,
    }


def enforce_single_position(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return trades
    selected = []
    free_at = -1
    ordered = trades.sort_values(
        ["entry_time", "initial_move"], key=lambda value: value.abs()
        if value.name == "initial_move"
        else value,
        ascending=[True, False],
    )
    for row in ordered.itertuples():
        if row.entry_time >= free_at:
            selected.append(row.Index)
            free_at = row.exit_time
    return ordered.loc[selected].sort_values("entry_time").reset_index(drop=True)


def metrics(returns: pd.Series) -> dict[str, float | int]:
    returns = returns.dropna()
    if returns.empty:
        return {
            "trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "net_return": 0.0,
            "mean_return": 0.0,
            "max_drawdown": 0.0,
        }
    gains = float(returns[returns > 0].sum())
    losses = float(-returns[returns < 0].sum())
    equity = (1.0 + returns.clip(lower=-0.999)).cumprod()
    drawdown = equity / equity.cummax() - 1.0
    return {
        "trades": int(len(returns)),
        "win_rate": float(returns.gt(0).mean()),
        "profit_factor": gains / losses if losses else 999.0,
        "net_return": float(returns.sum()),
        "mean_return": float(returns.mean()),
        "max_drawdown": float(drawdown.min()),
    }


def summarize(trades: pd.DataFrame) -> dict[str, object]:
    result: dict[str, object] = {}
    entry_time = pd.to_datetime(trades.entry_time, unit="ms", utc=True)
    for profile in PROFILES:
        scoped = trades.loc[trades.profile.eq(profile.name)].copy()
        scoped_time = entry_time.loc[scoped.index]
        profile_result = {}
        for window, (start, end) in WINDOWS.items():
            mask = scoped_time.ge(start) & scoped_time.lt(end)
            window_result = {}
            for label, cost in (
                ("base_cost", BASE_ROUND_TRIP_COST),
                ("stress_cost", STRESS_ROUND_TRIP_COST),
            ):
                window_result[label] = metrics(scoped.loc[mask, "gross_return"] - cost)
            profile_result[window] = window_result
        result[profile.name] = profile_result
    return result


def run(roots: list[Path]) -> tuple[pd.DataFrame, dict[str, object]]:
    raw: dict[str, list[dict[str, object]]] = {profile.name: [] for profile in PROFILES}
    names = available_names(roots)
    excluded_genesis = 0
    for completed, name in enumerate(names, start=1):
        hourly = load_symbol(name, roots)
        if hourly.empty:
            continue
        # The archive begins in 2020. A contract already present near the boundary has
        # unknown true age and is not a valid listing event.
        if int(hourly.open_time.iloc[0]) < int(
            pd.Timestamp("2020-02-01", tz="UTC").timestamp() * 1000
        ):
            excluded_genesis += 1
            continue
        opportunity = build_opportunity(hourly)
        if opportunity is not None:
            for profile in PROFILES:
                raw[profile.name].append(apply_exit(hourly, opportunity, profile))
        if completed % 100 == 0 or completed == len(names):
            print(f"audited symbols {completed}/{len(names)}", flush=True)
    selected = []
    for profile in PROFILES:
        selected.append(enforce_single_position(pd.DataFrame(raw[profile.name])))
    trades = pd.concat(selected, ignore_index=True)
    report = {
        "hypothesis": (
            "Fade the first 72 hours of a new Binance USD-M perpetual after a 2% move, "
            "provided trailing 24-hour quote volume is at least 20m USDT. Enter at the "
            "next hourly open and hold no longer than seven days."
        ),
        "research_status": (
            "Post-hoc candidate discovered in a daily diagnostic. No untouched holdout "
            "remains, so passing results are insufficient for live deployment."
        ),
        "symbols_seen": len(names),
        "genesis_symbols_excluded": excluded_genesis,
        "base_round_trip_cost": BASE_ROUND_TRIP_COST,
        "stress_round_trip_cost": STRESS_ROUND_TRIP_COST,
        "results": summarize(trades),
        "live_qualified": False,
        "live_qualification_reason": (
            "No protected exit profile produced positive after-cost returns across the "
            "ordered validation, test, blind, and final windows. The hypothesis was also "
            "discovered post hoc, so there is no untouched holdout left for promotion."
        ),
    }
    return trades, report


def main() -> None:
    args = parse_args()
    roots = [args.history, args.data, args.extension]
    trades, report = run(roots)
    args.output.mkdir(parents=True, exist_ok=True)
    trades.to_parquet(args.output / "trades.parquet", index=False)
    (args.output / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
