from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_s0_xmom_point_in_time import load_manifest  # noqa: E402


DEFAULT_DATA = (
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2024_2025"
)
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_donchian_ensemble"
LOOKBACKS = (5, 10, 20, 30)
TOP_ASSETS = 20
MINIMUM_AGE_DAYS = 365
MINIMUM_MEDIAN_DAILY_VOLUME = 2_000_000.0
MINIMUM_MEDIAN_ABS_RETURN = 0.005
ONE_WAY_COST = 0.0006


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Reproduce a fast Donchian trend ensemble and audit a single-"
            "position S0 adaptation on a point-in-time Binance universe."
        )
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def daily_symbol_frame(path: Path, start_ms: int) -> pd.DataFrame:
    frame = pd.read_parquet(
        path,
        columns=[
            "symbol",
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "quote_volume",
        ],
    ).sort_values("open_time")
    frame["day"] = pd.to_datetime(frame.open_time, unit="ms", utc=True).dt.floor(
        "D"
    )
    daily = (
        frame.groupby(["symbol", "day"], sort=True, as_index=False)
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            quote_volume=("quote_volume", "sum"),
        )
        .sort_values("day")
        .reset_index(drop=True)
    )
    daily["return"] = daily.close.pct_change()
    daily["next_return"] = daily.close.shift(-1) / daily.close - 1.0
    daily["return_30d"] = daily.close.pct_change(30)
    daily["volatility_90d"] = daily["return"].rolling(
        90, min_periods=60
    ).std() * math.sqrt(365)
    daily["median_volume_30d"] = daily.quote_volume.rolling(
        30, min_periods=20
    ).median()
    daily["median_abs_return_30d"] = daily["return"].abs().rolling(
        30, min_periods=20
    ).median()
    listed_at = pd.to_datetime(int(start_ms), unit="ms", utc=True)
    daily["symbol_age_days"] = (
        daily.day - listed_at
    ).dt.total_seconds() / 86_400
    for lookback in LOOKBACKS:
        upper = daily.close.rolling(lookback, min_periods=lookback).max()
        lower = daily.close.rolling(lookback, min_periods=lookback).min()
        midpoint = (upper + lower) / 2.0
        daily[f"position_{lookback}d"] = donchian_positions(
            daily.close,
            upper,
            midpoint,
        )
    position_columns = [f"position_{lookback}d" for lookback in LOOKBACKS]
    daily["ensemble_exposure"] = daily[position_columns].mean(axis=1)
    daily["volatility_scale"] = (
        0.25 / daily.volatility_90d.replace(0, np.nan)
    ).clip(upper=2.0)
    daily["target_exposure"] = (
        daily.ensemble_exposure * daily.volatility_scale
    ).fillna(0.0)
    return daily


def donchian_positions(
    close: pd.Series,
    upper: pd.Series,
    midpoint: pd.Series,
) -> pd.Series:
    positions: list[float] = []
    active = False
    trailing = float("nan")
    for price, upper_value, middle_value in zip(close, upper, midpoint):
        if not np.isfinite(upper_value) or not np.isfinite(middle_value):
            active = False
            trailing = float("nan")
            positions.append(0.0)
            continue
        if active and price <= trailing:
            active = False
            trailing = float("nan")
        if not active and price >= upper_value:
            active = True
            trailing = float(middle_value)
        elif active:
            trailing = max(trailing, float(middle_value))
        positions.append(1.0 if active else 0.0)
    return pd.Series(positions, index=close.index, dtype="float64")


def build_daily_panel(data: Path, starts: dict[str, int]) -> pd.DataFrame:
    parts = [
        daily_symbol_frame(path, starts[path.stem])
        for path in sorted((data / "parquet").glob("*.parquet"))
        if path.stem in starts
    ]
    if not parts:
        raise ValueError(f"No source parquet files found in {data}")
    return pd.concat(parts, ignore_index=True).sort_values(
        ["day", "symbol"]
    )


def monthly_universe(panel: pd.DataFrame) -> pd.DataFrame:
    months = pd.period_range(
        panel.day.min().tz_localize(None).to_period("M") + 1,
        panel.day.max().tz_localize(None).to_period("M"),
        freq="M",
    )
    records: list[dict[str, Any]] = []
    for month in months:
        boundary = month.start_time.tz_localize("UTC")
        history = panel.loc[panel.day.lt(boundary)]
        latest = history.groupby("symbol", sort=False).tail(1)
        eligible = latest.loc[
            latest.symbol_age_days.ge(MINIMUM_AGE_DAYS)
            & latest.median_volume_30d.ge(MINIMUM_MEDIAN_DAILY_VOLUME)
            & latest.median_abs_return_30d.ge(MINIMUM_MEDIAN_ABS_RETURN)
        ].nlargest(TOP_ASSETS, "median_volume_30d")
        for rank, row in enumerate(eligible.itertuples(index=False), start=1):
            records.append(
                {
                    "month": str(month),
                    "symbol": str(row.symbol),
                    "liquidity_rank": rank,
                    "selection_volume": float(row.median_volume_30d),
                }
            )
    return pd.DataFrame(
        records,
        columns=["month", "symbol", "liquidity_rank", "selection_volume"],
    )


def attach_universe(panel: pd.DataFrame, universe: pd.DataFrame) -> pd.DataFrame:
    result = panel.copy()
    result["month"] = result.day.dt.strftime("%Y-%m")
    return result.merge(universe, on=["month", "symbol"], how="inner")


def portfolio_returns(selected: pd.DataFrame) -> pd.DataFrame:
    if selected.empty:
        return pd.DataFrame()
    weights = selected.pivot(
        index="day", columns="symbol", values="target_exposure"
    ).fillna(0.0)
    counts = selected.groupby("day").symbol.nunique().reindex(weights.index)
    weights = weights.div(counts, axis=0)
    returns = selected.pivot(
        index="day", columns="symbol", values="next_return"
    ).reindex_like(weights).fillna(0.0)
    turnover = weights.diff().abs().sum(axis=1)
    turnover.iloc[0] = weights.iloc[0].abs().sum()
    gross = (weights * returns).sum(axis=1)
    return pd.DataFrame(
        {
            "day": weights.index,
            "gross_return": gross.values,
            "turnover": turnover.values,
            "net_return": (gross - turnover * ONE_WAY_COST).values,
        }
    )


def single_position_returns(selected: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    records: list[dict[str, Any]] = []
    current: str | None = None
    for day, frame in selected.groupby("day", sort=True):
        active = frame.loc[frame.ensemble_exposure.gt(0)].sort_values(
            ["ensemble_exposure", "return_30d", "median_volume_30d"],
            ascending=False,
        )
        active_symbols = set(active.symbol.astype(str))
        previous = current
        if current not in active_symbols:
            current = str(active.iloc[0].symbol) if not active.empty else None
        row = frame.loc[frame.symbol.eq(current)].head(1)
        gross = float(row.iloc[0].next_return) if not row.empty else 0.0
        turnover = 0.0
        if previous != current:
            turnover = float(previous is not None) + float(current is not None)
        records.append(
            {
                "day": day,
                "symbol": current,
                "gross_return": gross,
                "turnover": turnover,
                "net_return": gross - turnover * ONE_WAY_COST,
            }
        )
    daily = pd.DataFrame(records)
    trades = extract_single_position_trades(daily)
    return daily, trades


def extract_single_position_trades(daily: pd.DataFrame) -> pd.DataFrame:
    trades: list[dict[str, Any]] = []
    current: str | None = None
    entry_day: Any = None
    compounded = 1.0
    for row in daily.itertuples(index=False):
        symbol = None if pd.isna(row.symbol) else str(row.symbol)
        if symbol != current:
            if current is not None:
                trades.append(
                    {
                        "symbol": current,
                        "entry_day": entry_day,
                        "exit_day": row.day,
                        "net_return": compounded - 1.0 - ONE_WAY_COST,
                    }
                )
            current = symbol
            entry_day = row.day if symbol is not None else None
            compounded = 1.0
        if current is not None:
            compounded *= 1.0 + float(row.gross_return)
    if current is not None:
        trades.append(
            {
                "symbol": current,
                "entry_day": entry_day,
                "exit_day": daily.iloc[-1].day,
                "net_return": compounded - 1.0 - ONE_WAY_COST,
            }
        )
    return pd.DataFrame(trades)


def return_metrics(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {"days": 0, "total_return_pct": 0.0}
    returns = frame.net_return.fillna(0.0)
    equity = (1.0 + returns).cumprod()
    drawdown = equity / equity.cummax() - 1.0
    volatility = float(returns.std())
    return {
        "days": int(len(frame)),
        "total_return_pct": round(float((equity.iloc[-1] - 1.0) * 100), 6),
        "annualized_return_pct": round(
            float((equity.iloc[-1] ** (365 / len(frame)) - 1.0) * 100), 6
        ),
        "annualized_volatility_pct": round(volatility * math.sqrt(365) * 100, 6),
        "sharpe": round(
            float(returns.mean() / volatility * math.sqrt(365)), 6
        )
        if volatility > 0
        else 0.0,
        "max_drawdown_pct": round(float(drawdown.min() * 100), 6),
        "turnover": round(float(frame.turnover.sum()), 6),
    }


def trade_metrics(trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty:
        return {"trades": 0, "profit_factor": 0.0}
    winners = trades.net_return.clip(lower=0).sum()
    losers = -trades.net_return.clip(upper=0).sum()
    return {
        "trades": int(len(trades)),
        "symbols": int(trades.symbol.nunique()),
        "win_rate_pct": round(float(trades.net_return.gt(0).mean() * 100), 6),
        "profit_factor": round(float(winners / losers), 6) if losers else 999.0,
        "net_return_sum_pct": round(float(trades.net_return.sum() * 100), 6),
    }


def windowed_report(
    daily: pd.DataFrame,
    trades: pd.DataFrame | None = None,
) -> dict[str, Any]:
    windows = {
        "development_2024h2": ("2024-07-01", "2025-01-01"),
        "validation_2025h1": ("2025-01-01", "2025-07-01"),
        "test_2025h2": ("2025-07-01", "2026-01-01"),
    }
    result: dict[str, Any] = {}
    for name, (start, end) in windows.items():
        mask = daily.day.ge(start) & daily.day.lt(end)
        item: dict[str, Any] = {"daily": return_metrics(daily.loc[mask])}
        if trades is not None and not trades.empty:
            trade_mask = trades.entry_day.ge(start) & trades.entry_day.lt(end)
            item["trades"] = trade_metrics(trades.loc[trade_mask])
        result[name] = item
    return result


def main() -> None:
    args = parse_args()
    starts, manifest = load_manifest(args.data)
    if manifest.get("failures"):
        raise ValueError("Source manifest contains failures")
    panel = build_daily_panel(args.data, starts)
    universe = monthly_universe(panel)
    selected = attach_universe(panel, universe)
    portfolio = portfolio_returns(selected)
    single_daily, single_trades = single_position_returns(selected)
    portfolio_report = windowed_report(portfolio)
    single_report = windowed_report(single_daily, single_trades)
    qualified = all(
        single_report[name]["daily"].get("total_return_pct", 0) > 0
        and single_report[name]["trades"].get("profit_factor", 0) > 1.0
        for name in ("validation_2025h1", "test_2025h2")
    )
    report = {
        "experiment": "s0_donchian_fast_ensemble",
        "source": manifest.get("source"),
        "checksum_verified": manifest.get("checksum_verified"),
        "paper_replication_scope": {
            "lookbacks_days": list(LOOKBACKS),
            "top_assets": TOP_ASSETS,
            "long_only": True,
            "minimum_age_days": MINIMUM_AGE_DAYS,
            "one_way_cost_pct": ONE_WAY_COST * 100,
        },
        "important_difference": (
            "The source paper uses a diversified spot portfolio. The S0 result "
            "is a separately labelled single-position adaptation and cannot "
            "inherit the paper's claimed performance."
        ),
        "symbols": int(panel.symbol.nunique()),
        "portfolio_replication": portfolio_report,
        "single_position_s0_adaptation": single_report,
        "qualified_for_minute_validation": qualified,
        "decision": (
            "proceed_to_minute_validation"
            if qualified
            else "research_only_not_eligible"
        ),
    }
    args.output.mkdir(parents=True, exist_ok=True)
    universe.to_parquet(args.output / "monthly_universe.parquet", index=False)
    portfolio.to_parquet(args.output / "portfolio_daily.parquet", index=False)
    single_daily.to_parquet(args.output / "single_daily.parquet", index=False)
    single_trades.to_parquet(args.output / "single_trades.parquet", index=False)
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
