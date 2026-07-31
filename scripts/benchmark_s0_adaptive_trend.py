from __future__ import annotations

import argparse
import json
import math
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = (
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2024_2025"
)
DEFAULT_EXTENSION = (
    ROOT / "data" / "research" / "binance_um_point_in_time_1h"
)
DEFAULT_HISTORY = (
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2020_2023"
)
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_adaptive_trend"
BASE_ONE_WAY_COST = 0.0006
STRESS_ONE_WAY_COST = 0.0012
MINIMUM_MONTHLY_QUOTE_VOLUME = 30_000_000.0


@dataclass(frozen=True)
class Parameters:
    lookback: int
    threshold: float
    atr_multiplier: float
    atr_period: int = 14

    @property
    def name(self) -> str:
        return (
            f"l{self.lookback}_t{self.threshold:.3f}_"
            f"a{self.atr_multiplier:.1f}"
        )


# The paper omits its grid, ATR period, and K_S. This compact grid is fixed
# before seeing validation/test results and covers the reported alpha plateau.
PARAMETER_GRID = tuple(
    Parameters(lookback, threshold, multiplier)
    for lookback in (4, 8, 12)
    for threshold in (0.02, 0.04)
    for multiplier in (2.0, 2.5, 3.0)
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit an executable point-in-time proxy of the published "
            "AdaptiveTrend cryptocurrency strategy."
        )
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--extension", type=Path, default=DEFAULT_EXTENSION)
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--top-liquid", type=int, default=60)
    parser.add_argument("--long-candidates", type=int, default=15)
    parser.add_argument("--short-candidates", type=int, default=15)
    parser.add_argument("--workers", type=int, default=4)
    return parser.parse_args()


def load_symbol(
    path: Path,
    extension: Path | None = None,
    history: Path | None = None,
) -> pd.DataFrame:
    columns = [
        "symbol",
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "quote_volume",
    ]
    source_paths = [path]
    for source in (history, extension):
        if source is not None:
            extra = source / "parquet" / path.name
            if extra.exists() and extra not in source_paths:
                source_paths.append(extra)
    parts = [pd.read_parquet(source, columns=columns) for source in source_paths]
    hourly = (
        pd.concat(parts, ignore_index=True)
        .drop_duplicates("open_time", keep="last")
        .sort_values("open_time")
    )
    hourly["time"] = pd.to_datetime(hourly.open_time, unit="ms", utc=True)
    six_hour = (
        hourly.set_index("time")
        .resample("6h", label="left", closed="left")
        .agg(
            symbol=("symbol", "first"),
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            quote_volume=("quote_volume", "sum"),
            hourly_rows=("open_time", "count"),
        )
        .dropna(subset=["symbol", "open", "high", "low", "close"])
        .reset_index()
    )
    # Partial bars would make the historical signal non-comparable.
    return six_hour.loc[six_hour.hourly_rows.eq(6)].reset_index(drop=True)


def add_indicators(frame: pd.DataFrame, parameters: Parameters) -> pd.DataFrame:
    result = frame.copy()
    previous_close = result.close.shift(1)
    true_range = pd.concat(
        [
            result.high - result.low,
            (result.high - previous_close).abs(),
            (result.low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    # Every value used for an entry or a pre-bar stop is shifted by one bar.
    result["signal_momentum"] = result.close.pct_change(
        parameters.lookback
    ).shift(1)
    result["signal_atr"] = true_range.rolling(
        parameters.atr_period,
        min_periods=parameters.atr_period,
    ).mean().shift(1)
    return result


def simulate_direction(
    frame: pd.DataFrame,
    parameters: Parameters,
    direction: str,
    one_way_cost: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    data = add_indicators(frame, parameters)
    sign = 1.0 if direction == "LONG" else -1.0
    position = False
    entry_price = 0.0
    entry_time: pd.Timestamp | None = None
    stop = float("nan")
    bar_records: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []

    for row in data.itertuples(index=False):
        gross = 0.0
        entry = False
        exit_ = False
        exit_reason = ""
        price_open = float(row.open)
        price_close = float(row.close)
        atr = float(row.signal_atr) if np.isfinite(row.signal_atr) else np.nan
        momentum = (
            float(row.signal_momentum)
            if np.isfinite(row.signal_momentum)
            else np.nan
        )

        if position:
            stop_hit = (
                direction == "LONG" and float(row.low) <= stop
            ) or (
                direction == "SHORT" and float(row.high) >= stop
            )
            if stop_hit:
                # Adverse opening gaps cannot receive the more favorable stop.
                exit_price = (
                    min(price_open, stop)
                    if direction == "LONG"
                    else max(price_open, stop)
                )
                gross = sign * (exit_price / entry_price - 1.0)
                exit_ = True
                exit_reason = "trailing_stop"
            else:
                gross = sign * (price_close / entry_price - 1.0)
                if np.isfinite(atr):
                    candidate = (
                        price_close - parameters.atr_multiplier * atr
                        if direction == "LONG"
                        else price_close + parameters.atr_multiplier * atr
                    )
                    stop = (
                        max(stop, candidate)
                        if direction == "LONG"
                        else min(stop, candidate)
                    )

            if exit_:
                net = gross - 2.0 * one_way_cost
                trades.append(
                    {
                        "symbol": str(row.symbol),
                        "direction": direction,
                        "parameters": parameters.name,
                        "entry_time": entry_time,
                        "exit_time": row.time,
                        "gross_return": gross,
                        "net_return": net,
                        "exit_reason": exit_reason,
                    }
                )
                position = False
                entry_price = 0.0
                entry_time = None
                stop = float("nan")

        entry_signal = np.isfinite(momentum) and (
            momentum > parameters.threshold
            if direction == "LONG"
            else momentum < -parameters.threshold
        )
        if not position and not exit_ and entry_signal and np.isfinite(atr):
            position = True
            entry = True
            entry_price = price_open
            entry_time = row.time
            stop = (
                price_open - parameters.atr_multiplier * atr
                if direction == "LONG"
                else price_open + parameters.atr_multiplier * atr
            )

        bar_records.append(
            {
                "time": row.time,
                "symbol": str(row.symbol),
                "direction": direction,
                "parameters": parameters.name,
                "active": position,
                "entry": entry,
                "exit": exit_,
            }
        )

    return pd.DataFrame(bar_records), pd.DataFrame(trades)


def trade_metrics(trades: pd.DataFrame) -> dict[str, float | int]:
    if trades.empty:
        return {
            "trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "net_return": 0.0,
        }
    wins = trades.loc[trades.net_return.gt(0), "net_return"].sum()
    losses = -trades.loc[trades.net_return.lt(0), "net_return"].sum()
    return {
        "trades": int(len(trades)),
        "win_rate": float(trades.net_return.gt(0).mean()),
        "profit_factor": float(wins / losses) if losses > 0 else 999.0,
        "net_return": float(trades.net_return.sum()),
    }


def prior_month_selection(
    all_trades: pd.DataFrame,
    liquidity: pd.DataFrame,
    top_liquid: int,
    long_candidates: int,
    short_candidates: int,
) -> pd.DataFrame:
    trades_with_month = all_trades.copy()
    trades_with_month["selection_month"] = (
        trades_with_month.entry_time.dt.strftime("%Y-%m")
    )
    records: list[dict[str, Any]] = []
    months = sorted(liquidity.month.unique())
    for month in months[1:]:
        period = pd.Period(month, freq="M")
        prior = str(period - 1)
        ranked = (
            liquidity.loc[liquidity.month.eq(prior)]
            .sort_values("quote_volume", ascending=False)
            .head(top_liquid)
            .reset_index(drop=True)
        )
        if ranked.empty:
            continue
        long_universe = set(ranked.head(long_candidates).symbol)
        short_universe = set(ranked.tail(short_candidates).symbol)
        history = trades_with_month.loc[
            trades_with_month.selection_month.eq(prior)
        ].copy()
        if history.empty:
            continue
        grouped = history.groupby(
            ["symbol", "direction", "parameters"], sort=False
        )
        scores = grouped.net_return.agg(["count", "mean", "std", "sum"])
        scores = scores.reset_index()
        scores["sharpe"] = (
            scores["mean"]
            / scores["std"].replace(0, np.nan)
            * np.sqrt(scores["count"].clip(lower=1))
        ).fillna(0.0)
        scores = scores.loc[scores["count"].ge(2)]
        best = (
            scores.sort_values(
                ["symbol", "direction", "sharpe", "sum"],
                ascending=[True, True, False, False],
            )
            .groupby(["symbol", "direction"], sort=False)
            .head(1)
        )
        for row in best.itertuples(index=False):
            allowed = (
                row.direction == "LONG" and row.symbol in long_universe
            ) or (
                row.direction == "SHORT" and row.symbol in short_universe
            )
            threshold = 1.3 if row.direction == "LONG" else 1.7
            if allowed and row.sharpe >= threshold:
                records.append(
                    {
                        "month": month,
                        "symbol": row.symbol,
                        "direction": row.direction,
                        "parameters": row.parameters,
                        "prior_sharpe": float(row.sharpe),
                        "prior_trades": int(row.count),
                    }
                )
    return pd.DataFrame(records)


def select_s0_trades(
    all_trades: pd.DataFrame,
    selection: pd.DataFrame,
) -> pd.DataFrame:
    if selection.empty:
        return pd.DataFrame()
    trades = all_trades.copy()
    trades["month"] = trades.entry_time.dt.strftime("%Y-%m")
    eligible = trades.merge(
        selection,
        on=["month", "symbol", "direction", "parameters"],
        how="inner",
    ).sort_values(["entry_time", "prior_sharpe"], ascending=[True, False])
    chosen: list[pd.Series] = []
    available_at = pd.Timestamp.min.tz_localize("UTC")
    for _, row in eligible.iterrows():
        if row.entry_time >= available_at:
            chosen.append(row)
            available_at = row.exit_time
    return pd.DataFrame(chosen).reset_index(drop=True)


def window_metrics(trades: pd.DataFrame) -> dict[str, dict[str, float | int]]:
    windows = {
        "development_2020_2022": ("2020-02-01", "2023-01-01"),
        "validation_2023": ("2023-01-01", "2024-01-01"),
        "test_2024": ("2024-01-01", "2025-01-01"),
        "blind_2025": ("2025-01-01", "2026-01-01"),
        "final_blind_2026_h1": ("2026-01-01", "2026-07-01"),
    }
    output: dict[str, dict[str, float | int]] = {}
    for name, (start, end) in windows.items():
        subset = trades.loc[
            trades.entry_time.ge(pd.Timestamp(start, tz="UTC"))
            & trades.entry_time.lt(pd.Timestamp(end, tz="UTC"))
        ]
        output[name] = trade_metrics(subset)
    return output


def concentration_robustness(
    trades: pd.DataFrame,
) -> dict[str, dict[str, Any]]:
    windows = {
        "development_2020_2022": ("2020-02-01", "2023-01-01"),
        "validation_2023": ("2023-01-01", "2024-01-01"),
        "test_2024": ("2024-01-01", "2025-01-01"),
        "blind_2025": ("2025-01-01", "2026-01-01"),
        "final_blind_2026_h1": ("2026-01-01", "2026-07-01"),
    }
    output: dict[str, dict[str, Any]] = {}
    for name, (start, end) in windows.items():
        subset = trades.loc[
            trades.entry_time.ge(pd.Timestamp(start, tz="UTC"))
            & trades.entry_time.lt(pd.Timestamp(end, tz="UTC"))
        ].copy()
        if subset.empty:
            output[name] = {
                "best_trade": None,
                "without_best_trade": trade_metrics(subset),
                "returns_capped_at_50pct": trade_metrics(subset),
            }
            continue
        best_index = subset.net_return.idxmax()
        best = subset.loc[best_index]
        capped = subset.copy()
        capped["net_return"] = capped.net_return.clip(upper=0.50)
        output[name] = {
            "best_trade": {
                "symbol": str(best.symbol),
                "net_return": float(best.net_return),
            },
            "without_best_trade": trade_metrics(subset.drop(best_index)),
            "returns_capped_at_50pct": trade_metrics(capped),
        }
    return output


def iter_paths(data: Path) -> Iterable[Path]:
    return sorted((data / "parquet").glob("*.parquet"))


def monthly_liquidity(
    path: Path,
    extension: Path | None,
    history: Path | None = None,
) -> pd.DataFrame:
    columns = ["symbol", "open_time", "quote_volume"]
    source_paths = [path]
    for source in (history, extension):
        if source is not None:
            extra = source / "parquet" / path.name
            if extra.exists() and extra not in source_paths:
                source_paths.append(extra)
    parts = [pd.read_parquet(source, columns=columns) for source in source_paths]
    hourly = (
        pd.concat(parts, ignore_index=True)
        .drop_duplicates("open_time", keep="last")
    )
    hourly["month"] = pd.to_datetime(
        hourly.open_time, unit="ms", utc=True
    ).dt.strftime("%Y-%m")
    return hourly.groupby(["month", "symbol"], as_index=False).quote_volume.sum()


def simulate_symbol_path(
    path: Path,
    extension: Path | None,
    history: Path | None,
) -> pd.DataFrame:
    frame = load_symbol(path, extension, history)
    if len(frame) < 120:
        return pd.DataFrame()
    parts: list[pd.DataFrame] = []
    for parameters in PARAMETER_GRID:
        for direction in ("LONG", "SHORT"):
            _, trades = simulate_direction(
                frame,
                parameters,
                direction,
                BASE_ONE_WAY_COST,
            )
            if not trades.empty:
                parts.append(trades)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def main() -> None:
    args = parse_args()
    source_roots = [
        source
        for source in (args.data, args.history, args.extension)
        if source is not None
    ]
    names = sorted(
        {
            path.name
            for source in source_roots
            for path in iter_paths(source)
        }
    )
    paths = [
        next(
            source / "parquet" / name
            for source in source_roots
            if (source / "parquet" / name).exists()
        )
        for name in names
    ]
    if not paths:
        raise ValueError(f"No parquet files found in {args.data}")

    all_trades: list[pd.DataFrame] = []
    liquidity_parts: list[pd.DataFrame] = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                monthly_liquidity, path, args.extension, args.history
            ): path.stem
            for path in paths
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            liquidity_parts.append(future.result())
            if completed % 25 == 0 or completed == len(futures):
                print(
                    f"profiled liquidity {completed}/{len(futures)}",
                    flush=True,
                )
    liquidity = pd.concat(liquidity_parts, ignore_index=True)
    liquidity = liquidity.loc[
        liquidity.quote_volume.ge(MINIMUM_MONTHLY_QUOTE_VOLUME)
    ].copy()
    liquid_symbols = set(
        liquidity.sort_values(
            ["month", "quote_volume"], ascending=[True, False]
        )
        .groupby("month", sort=False)
        .head(args.top_liquid)
        .symbol.astype(str)
    )
    selected_paths = [path for path in paths if path.stem in liquid_symbols]

    symbols_loaded = 0
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                simulate_symbol_path, path, args.extension, args.history
            ): path.stem
            for path in selected_paths
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            symbol_trades = future.result()
            if not symbol_trades.empty:
                symbols_loaded += 1
                all_trades.append(symbol_trades)
            if completed % 25 == 0 or completed == len(futures):
                print(
                    f"simulated {completed}/{len(futures)} liquid symbols",
                    flush=True,
                )

    trades = pd.concat(all_trades, ignore_index=True)
    selection = prior_month_selection(
        trades,
        liquidity,
        args.top_liquid,
        args.long_candidates,
        args.short_candidates,
    )
    chosen = select_s0_trades(trades, selection)
    base_windows = window_metrics(chosen)
    concentration = concentration_robustness(chosen)
    stressed = chosen.copy()
    if not stressed.empty:
        stressed["net_return"] = (
            stressed.gross_return - 2.0 * STRESS_ONE_WAY_COST
        )
    stress_windows = window_metrics(stressed)

    oos_names = (
        "validation_2023",
        "test_2024",
        "blind_2025",
        "final_blind_2026_h1",
    )
    accepted = bool(
        all(base_windows[name]["trades"] >= 8 for name in oos_names)
        and all(base_windows[name]["net_return"] > 0 for name in oos_names)
        and all(stress_windows[name]["net_return"] > 0 for name in oos_names)
        and all(base_windows[name]["profit_factor"] >= 1.10 for name in oos_names)
        and all(
            concentration[name]["without_best_trade"]["net_return"] > 0
            for name in oos_names
        )
        and all(
            concentration[name]["returns_capped_at_50pct"]["profit_factor"]
            >= 1.05
            for name in oos_names
        )
    )
    report = {
        "experiment": "s0_adaptive_trend_point_in_time_audit",
        "source_method": "AdaptiveTrend paper proxy",
        "reproducibility_limits": [
            "Paper does not publish K_S.",
            "Paper does not publish the full parameter grid.",
            "Paper does not publish the ATR period.",
            "Point-in-time market cap is replaced by prior-month quote volume.",
        ],
        "execution": {
            "signal": "completed 6h bar",
            "entry": "next 6h open",
        "intrabar_stop_path": "6h high/low aggregated from official 1h bars",
            "base_round_trip_cost": 2.0 * BASE_ONE_WAY_COST,
            "stress_round_trip_cost": 2.0 * STRESS_ONE_WAY_COST,
        },
        "parameter_grid": [p.__dict__ for p in PARAMETER_GRID],
        "symbols_loaded": symbols_loaded,
        "selection_rows": int(len(selection)),
        "s0_non_overlapping_trades": int(len(chosen)),
        "base": base_windows,
        "stress": stress_windows,
        "concentration_robustness": concentration,
        "accepted": accepted,
        "decision": (
            "eligible_for_further_shadow_validation"
            if accepted
            else "rejected_not_positive_expectancy"
        ),
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    chosen.to_parquet(args.output / "s0_trades.parquet", index=False)
    selection.to_parquet(args.output / "monthly_selection.parquet", index=False)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
