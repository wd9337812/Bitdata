from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_s0_donchian_ensemble import (  # noqa: E402
    DEFAULT_DATA,
    MINIMUM_AGE_DAYS,
    MINIMUM_MEDIAN_ABS_RETURN,
    MINIMUM_MEDIAN_DAILY_VOLUME,
    ONE_WAY_COST,
    attach_universe,
    build_daily_panel,
    monthly_universe,
    return_metrics,
    trade_metrics,
)
from scripts.benchmark_s0_xmom_point_in_time import load_manifest  # noqa: E402


DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_donchian_long_short"


@dataclass(frozen=True)
class Ensemble:
    name: str
    lookbacks: tuple[int, ...]


ENSEMBLES = (
    Ensemble("fast_5_10_20_30", (5, 10, 20, 30)),
    Ensemble("medium_10_20_30_60", (10, 20, 30, 60)),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark symmetric long-short Donchian time-series momentum "
            "with development-only ensemble selection."
        )
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def long_short_positions(
    close: pd.Series,
    upper: pd.Series,
    lower: pd.Series,
    midpoint: pd.Series,
) -> pd.Series:
    positions: list[float] = []
    position = 0
    trailing = float("nan")
    for price, upper_value, lower_value, middle_value in zip(
        close, upper, lower, midpoint
    ):
        if not all(
            np.isfinite(value)
            for value in (upper_value, lower_value, middle_value)
        ):
            position = 0
            trailing = float("nan")
            positions.append(0.0)
            continue
        if position > 0 and price <= trailing:
            position = 0
        elif position < 0 and price >= trailing:
            position = 0
        if position == 0:
            if price >= upper_value:
                position = 1
                trailing = float(middle_value)
            elif price <= lower_value:
                position = -1
                trailing = float(middle_value)
        elif position > 0:
            trailing = max(trailing, float(middle_value))
        else:
            trailing = min(trailing, float(middle_value))
        positions.append(float(position))
    return pd.Series(positions, index=close.index, dtype="float64")


def add_ensemble(panel: pd.DataFrame, ensemble: Ensemble) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for _, symbol_frame in panel.groupby("symbol", sort=False):
        frame = symbol_frame.sort_values("day").copy()
        columns: list[str] = []
        for lookback in ensemble.lookbacks:
            upper = frame.close.rolling(lookback, min_periods=lookback).max()
            lower = frame.close.rolling(lookback, min_periods=lookback).min()
            midpoint = (upper + lower) / 2.0
            column = f"ls_position_{lookback}d"
            frame[column] = long_short_positions(
                frame.close,
                upper,
                lower,
                midpoint,
            )
            columns.append(column)
        frame["ensemble_signal"] = frame[columns].mean(axis=1)
        frame["ensemble_confidence"] = frame.ensemble_signal.abs()
        parts.append(frame)
    return pd.concat(parts, ignore_index=True).sort_values(["day", "symbol"])


def single_position_returns(
    selected: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    records: list[dict[str, Any]] = []
    current_symbol: str | None = None
    current_direction = 0
    for day, frame in selected.groupby("day", sort=True):
        active = frame.loc[frame.ensemble_confidence.gt(0)].copy()
        active["normalized_trend"] = (
            active.return_30d.abs()
            / active.volatility_90d.replace(0, np.nan)
        ).fillna(0.0)
        active = active.sort_values(
            ["ensemble_confidence", "normalized_trend", "median_volume_30d"],
            ascending=False,
        )
        previous_symbol = current_symbol
        previous_direction = current_direction
        current_row = frame.loc[frame.symbol.eq(current_symbol)].head(1)
        still_active = bool(
            not current_row.empty
            and np.sign(float(current_row.iloc[0].ensemble_signal))
            == current_direction
            and float(current_row.iloc[0].ensemble_confidence) > 0
        )
        if not still_active:
            if active.empty:
                current_symbol = None
                current_direction = 0
            else:
                current_symbol = str(active.iloc[0].symbol)
                current_direction = int(np.sign(active.iloc[0].ensemble_signal))
        row = frame.loc[frame.symbol.eq(current_symbol)].head(1)
        gross = (
            current_direction * float(row.iloc[0].next_return)
            if not row.empty
            else 0.0
        )
        turnover = 0.0
        if (previous_symbol, previous_direction) != (
            current_symbol,
            current_direction,
        ):
            turnover = float(previous_symbol is not None) + float(
                current_symbol is not None
            )
        records.append(
            {
                "day": day,
                "symbol": current_symbol,
                "direction": current_direction,
                "gross_return": gross,
                "turnover": turnover,
                "net_return": gross - turnover * ONE_WAY_COST,
            }
        )
    daily = pd.DataFrame(records)
    return daily, extract_trades(daily)


def extract_trades(daily: pd.DataFrame) -> pd.DataFrame:
    trades: list[dict[str, Any]] = []
    current_key: tuple[str, int] | None = None
    entry_day: Any = None
    compounded = 1.0
    for row in daily.itertuples(index=False):
        key = (
            (str(row.symbol), int(row.direction))
            if not pd.isna(row.symbol) and int(row.direction) != 0
            else None
        )
        if key != current_key:
            if current_key is not None:
                trades.append(
                    {
                        "symbol": current_key[0],
                        "direction": current_key[1],
                        "entry_day": entry_day,
                        "exit_day": row.day,
                        "net_return": compounded - 1.0 - ONE_WAY_COST,
                    }
                )
            current_key = key
            entry_day = row.day if key else None
            compounded = 1.0
        if current_key is not None:
            compounded *= 1.0 + float(row.gross_return)
    if current_key is not None:
        trades.append(
            {
                "symbol": current_key[0],
                "direction": current_key[1],
                "entry_day": entry_day,
                "exit_day": daily.iloc[-1].day,
                "net_return": compounded - 1.0 - ONE_WAY_COST,
            }
        )
    return pd.DataFrame(trades)


def window(
    daily: pd.DataFrame,
    trades: pd.DataFrame,
    start: str,
    end: str,
) -> dict[str, Any]:
    daily_mask = daily.day.ge(start) & daily.day.lt(end)
    trade_mask = trades.entry_day.ge(start) & trades.entry_day.lt(end)
    return {
        "daily": return_metrics(daily.loc[daily_mask]),
        "trades": trade_metrics(trades.loc[trade_mask]),
    }


def evaluate(
    panel: pd.DataFrame,
    universe: pd.DataFrame,
    ensemble: Ensemble,
) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame]:
    enriched = add_ensemble(panel, ensemble)
    selected = attach_universe(enriched, universe)
    daily, trades = single_position_returns(selected)
    return {
        "ensemble": asdict(ensemble),
        "development_2024h2": window(
            daily, trades, "2024-07-01", "2025-01-01"
        ),
        "validation_2025h1": window(
            daily, trades, "2025-01-01", "2025-07-01"
        ),
        "test_2025h2": window(daily, trades, "2025-07-01", "2026-01-01"),
    }, daily, trades


def development_score(report: dict[str, Any]) -> tuple[float, float]:
    metrics = report["development_2024h2"]
    return (
        float(metrics["trades"].get("profit_factor", 0)),
        float(metrics["daily"].get("total_return_pct", 0)),
    )


def main() -> None:
    args = parse_args()
    starts, manifest = load_manifest(args.data)
    panel = build_daily_panel(args.data, starts)
    universe = monthly_universe(panel)
    reports: dict[str, Any] = {}
    outputs: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
    for ensemble in ENSEMBLES:
        report, daily, trades = evaluate(panel, universe, ensemble)
        reports[ensemble.name] = report
        outputs[ensemble.name] = (daily, trades)
    selected_name = max(reports, key=lambda name: development_score(reports[name]))
    selected = reports[selected_name]
    qualified = all(
        selected[name]["daily"].get("total_return_pct", 0) > 0
        and selected[name]["trades"].get("profit_factor", 0) > 1.0
        and selected[name]["trades"].get("trades", 0) >= 10
        for name in ("validation_2025h1", "test_2025h2")
    )
    result = {
        "experiment": "s0_donchian_long_short_time_series_momentum",
        "source": manifest.get("source"),
        "checksum_verified": manifest.get("checksum_verified"),
        "selection_window": "2024H2 only",
        "universe_rules": {
            "minimum_age_days": MINIMUM_AGE_DAYS,
            "minimum_median_daily_volume": MINIMUM_MEDIAN_DAILY_VOLUME,
            "minimum_median_abs_return": MINIMUM_MEDIAN_ABS_RETURN,
        },
        "candidates": reports,
        "selected_candidate": selected_name,
        "qualified_for_minute_validation": qualified,
        "decision": (
            "proceed_to_minute_validation"
            if qualified
            else "research_only_not_eligible"
        ),
    }
    args.output.mkdir(parents=True, exist_ok=True)
    for name, (daily, trades) in outputs.items():
        daily.to_parquet(args.output / f"{name}_daily.parquet", index=False)
        trades.to_parquet(args.output / f"{name}_trades.parquet", index=False)
    (args.output / "report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
