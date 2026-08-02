from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = (
    ROOT
    / "data"
    / "research"
    / "s0_cross_sectional_reversal"
    / "daily_panel.parquet"
)
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_market_relative_strength"
TRAIN_END = pd.Timestamp("2023-12-31", tz="UTC")
ONE_WAY_STRESS_COST = 0.0012
MINIMUM_AGE_DAYS = 90
TOP_LIQUID_SYMBOLS = 20
MINIMUM_MEDIAN_VOLUME = 5_000_000.0


@dataclass(frozen=True)
class Variant:
    market_fast_days: int
    market_slow_days: int
    asset_momentum_days: int
    hold_days: int
    stop_pct: float

    @property
    def name(self) -> str:
        return (
            f"m{self.market_fast_days}_{self.market_slow_days}"
            f"_a{self.asset_momentum_days}_h{self.hold_days}"
            f"_s{int(self.stop_pct * 1000):03d}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit market-regime plus point-in-time relative strength."
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def prepare_panel(path: Path) -> pd.DataFrame:
    frame = pd.read_parquet(path).rename(columns={"date": "day"})
    frame["day"] = pd.to_datetime(frame.day, utc=True)
    frame = frame.sort_values(["symbol", "day"]).reset_index(drop=True)
    grouped = frame.groupby("symbol", sort=False)
    frame["return"] = grouped.close.pct_change(fill_method=None)
    frame["median_volume_30d"] = grouped.quote_volume.transform(
        lambda values: values.rolling(30, min_periods=20).median()
    )
    first_days = grouped.day.transform("min")
    frame["age_days"] = (frame.day - first_days).dt.total_seconds() / 86_400
    for days in (14, 28, 42, 56, 84):
        frame[f"momentum_{days}d"] = grouped.close.pct_change(
            days, fill_method=None
        )
    return frame


def market_panel(panel: pd.DataFrame) -> pd.DataFrame:
    eligible = panel.loc[
        panel.age_days.ge(MINIMUM_AGE_DAYS)
        & panel.median_volume_30d.ge(MINIMUM_MEDIAN_VOLUME)
        & panel["return"].notna()
    ].copy()
    eligible["liquidity_rank"] = eligible.groupby(
        "day", sort=False
    ).median_volume_30d.rank(method="first", ascending=False)
    liquid = eligible.loc[
        eligible.liquidity_rank.le(TOP_LIQUID_SYMBOLS)
    ].copy()
    market = (
        liquid.groupby("day", as_index=False)
        .agg(
            market_return=("return", "mean"),
            universe_size=("symbol", "nunique"),
        )
        .sort_values("day")
        .reset_index(drop=True)
    )
    market["market_index"] = (1.0 + market.market_return).cumprod()
    for days in (14, 28, 42, 56, 84):
        market[f"market_momentum_{days}d"] = market.market_index.pct_change(days)
    return liquid, market


def training_threshold(market: pd.DataFrame, fast_days: int) -> float:
    values = market.loc[
        market.day.le(TRAIN_END), f"market_momentum_{fast_days}d"
    ].dropna()
    return float(values.quantile(2.0 / 3.0))


def simulate(
    panel: pd.DataFrame,
    liquid: pd.DataFrame,
    market: pd.DataFrame,
    variant: Variant,
    *,
    execution_delay_days: int = 0,
) -> pd.DataFrame:
    threshold = training_threshold(market, variant.market_fast_days)
    state = market.loc[
        market.universe_size.ge(TOP_LIQUID_SYMBOLS)
        & market[f"market_momentum_{variant.market_fast_days}d"].gt(threshold)
        & market[f"market_momentum_{variant.market_slow_days}d"].gt(0),
        [
            "day",
            f"market_momentum_{variant.market_fast_days}d",
            f"market_momentum_{variant.market_slow_days}d",
        ],
    ].copy()
    candidates = liquid.merge(state, on="day", how="inner")
    momentum_column = f"momentum_{variant.asset_momentum_days}d"
    slow_column = f"momentum_{variant.market_slow_days}d"
    candidates = candidates.loc[
        candidates[momentum_column].gt(0)
        & candidates[slow_column].gt(0)
    ].copy()
    candidates["relative_rank"] = candidates.groupby(
        "day", sort=False
    )[momentum_column].rank(pct=True)
    selected = (
        candidates.sort_values(
            ["day", momentum_column, "median_volume_30d"],
            ascending=[True, False, False],
        )
        .groupby("day", sort=False)
        .head(1)
        .sort_values("day")
    )

    paths = {
        symbol: group.set_index("day").sort_index()
        for symbol, group in panel.groupby("symbol", sort=False)
    }
    all_days = pd.Index(market.day.sort_values().unique())
    positions = {day: index for index, day in enumerate(all_days)}
    records: list[dict[str, Any]] = []
    available_day = pd.Timestamp.min.tz_localize("UTC")
    for row in selected.itertuples(index=False):
        signal_day = row.day
        signal_position = positions.get(signal_day)
        if signal_position is None:
            continue
        entry_position = signal_position + 1 + execution_delay_days
        final_position = entry_position + variant.hold_days
        if final_position >= len(all_days):
            continue
        entry_day = all_days[entry_position]
        if entry_day < available_day:
            continue
        symbol_path = paths[str(row.symbol)]
        if entry_day not in symbol_path.index:
            continue
        entry_price = float(symbol_path.loc[entry_day, "open"])
        if not math.isfinite(entry_price) or entry_price <= 0:
            continue
        stop_price = entry_price * (1.0 - variant.stop_pct)
        exit_day = all_days[final_position]
        exit_price: float | None = None
        exit_reason = "time"
        for path_position in range(entry_position, final_position + 1):
            day = all_days[path_position]
            if day not in symbol_path.index:
                exit_price = None
                break
            bar = symbol_path.loc[day]
            open_price = float(bar.open)
            low_price = float(bar.low)
            if open_price <= stop_price:
                exit_day = day
                exit_price = open_price
                exit_reason = "gap_stop"
                break
            if low_price <= stop_price:
                exit_day = day
                exit_price = stop_price
                exit_reason = "stop"
                break
        if exit_price is None:
            if exit_day not in symbol_path.index:
                continue
            exit_price = float(symbol_path.loc[exit_day, "open"])
        records.append(
            {
                "variant": variant.name,
                "symbol": str(row.symbol),
                "signal_day": signal_day,
                "entry_day": entry_day,
                "exit_day": exit_day,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "gross_return": exit_price / entry_price - 1.0,
                "exit_reason": exit_reason,
                "relative_rank": float(row.relative_rank),
                "market_fast": float(
                    getattr(row, f"market_momentum_{variant.market_fast_days}d")
                ),
                "market_slow": float(
                    getattr(row, f"market_momentum_{variant.market_slow_days}d")
                ),
                "threshold": threshold,
            }
        )
        available_day = exit_day
    return pd.DataFrame(records)


def metrics(
    trades: pd.DataFrame,
    *,
    risk_pct: float = 0.10,
    stop_pct: float = 0.10,
) -> dict[str, float | int]:
    if trades.empty:
        return {
            "trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "net_return": 0.0,
            "max_drawdown": 0.0,
        }
    exposure = min(3.0, risk_pct / stop_pct)
    net = trades.gross_return * exposure - 2.0 * ONE_WAY_STRESS_COST * exposure
    wins = float(net.clip(lower=0).sum())
    losses = float(-net.clip(upper=0).sum())
    equity = (1.0 + net).cumprod()
    drawdown = equity / equity.cummax() - 1.0
    return {
        "trades": int(len(trades)),
        "win_rate": round(float(net.gt(0).mean() * 100.0), 4),
        "profit_factor": round(wins / losses, 4) if losses else 999.0,
        "net_return": round(float(equity.iloc[-1] - 1.0), 6),
        "max_drawdown": round(float(drawdown.min()), 6),
        "exposure": round(float(exposure), 4),
    }


def report_windows(trades: pd.DataFrame, variant: Variant) -> dict[str, Any]:
    years = pd.to_datetime(trades.entry_day, utc=True).dt.year
    training = trades.loc[years.le(2023)]
    out_of_sample = trades.loc[years.ge(2024)]
    return {
        "training": metrics(training, stop_pct=variant.stop_pct),
        "out_of_sample": metrics(out_of_sample, stop_pct=variant.stop_pct),
        "annual": {
            str(int(year)): metrics(group, stop_pct=variant.stop_pct)
            for year, group in trades.assign(year=years).groupby("year", sort=True)
        },
    }


def training_score(report: dict[str, Any]) -> tuple[float, float, float]:
    annual = report["annual"]
    years = [annual.get(str(year), {}) for year in range(2020, 2024)]
    positive_years = sum(float(item.get("net_return", 0)) > 0 for item in years)
    worst_year = min(float(item.get("net_return", -1)) for item in years)
    training = report["training"]
    return (
        float(positive_years),
        worst_year,
        float(training.get("profit_factor", 0)),
    )


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    panel = prepare_panel(args.data)
    liquid, market = market_panel(panel)
    variants = [
        Variant(fast, slow, asset, hold, stop)
        for fast in (14, 28, 42)
        for slow in (56, 84)
        if slow > fast
        for asset in (14, 28, 56)
        for hold in (3, 5, 7)
        for stop in (0.08, 0.10, 0.12)
    ]
    reports: dict[str, Any] = {}
    trades_by_variant: dict[str, pd.DataFrame] = {}
    for variant in variants:
        trades = simulate(panel, liquid, market, variant)
        trades_by_variant[variant.name] = trades
        reports[variant.name] = {
            "parameters": asdict(variant),
            **report_windows(trades, variant),
        }
    selected_name = max(reports, key=lambda name: training_score(reports[name]))
    selected = next(item for item in variants if item.name == selected_name)
    selected_trades = trades_by_variant[selected_name]
    delay_reports = {
        str(delay): report_windows(
            simulate(
                panel,
                liquid,
                market,
                selected,
                execution_delay_days=delay,
            ),
            selected,
        )["out_of_sample"]
        for delay in (0, 1, 2)
    }
    risk_sweep = {
        str(risk): metrics(
            selected_trades,
            risk_pct=risk,
            stop_pct=selected.stop_pct,
        )
        for risk in (0.05, 0.10, 0.15, 0.20, 0.30)
    }
    selected_report = reports[selected_name]
    annual = selected_report["annual"]
    accepted = bool(
        selected_report["training"]["profit_factor"] > 1.10
        and selected_report["out_of_sample"]["profit_factor"] > 1.10
        and selected_report["out_of_sample"]["net_return"] > 0
        and all(
            float(annual.get(str(year), {}).get("net_return", -1)) > 0
            for year in range(2024, 2027)
        )
        and all(
            result["profit_factor"] > 1.0 and result["net_return"] > 0
            for result in delay_reports.values()
        )
    )
    output = {
        "experiment": "s0_market_relative_strength",
        "data": str(args.data),
        "point_in_time_rules": {
            "minimum_age_days": MINIMUM_AGE_DAYS,
            "minimum_median_volume": MINIMUM_MEDIAN_VOLUME,
            "top_liquid_symbols": TOP_LIQUID_SYMBOLS,
            "entry": "next UTC day open after signal",
            "cost": ONE_WAY_STRESS_COST,
        },
        "selection": (
            "parameters selected only on 2020-2023 by positive-year count, "
            "worst-year return, then profit factor"
        ),
        "selected_variant": selected_name,
        "selected_report": selected_report,
        "execution_delay_oos": delay_reports,
        "risk_sweep_full_period": risk_sweep,
        "accepted": accepted,
        "decision": (
            "eligible_for_realtime_shadow"
            if accepted
            else "rejected_not_stable_positive_expectancy"
        ),
        "all_variants": reports,
    }
    selected_trades.to_parquet(
        args.output / "selected_trades.parquet", index=False, compression="zstd"
    )
    (args.output / "report.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({key: value for key, value in output.items() if key != "all_variants"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
