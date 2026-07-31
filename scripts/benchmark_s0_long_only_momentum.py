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

from scripts.benchmark_s0_category_momentum import (  # noqa: E402
    DEFAULT_AUDIT_DATA,
    DEFAULT_DATA,
    load_daily_panel,
)
from scripts.benchmark_s0_donchian_ensemble import ONE_WAY_COST  # noqa: E402


DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_long_only_momentum"
FORMATION_DAYS = 30
HOLD_DAYS = 7
MINIMUM_AGE_DAYS = 90
MINIMUM_MEDIAN_VOLUME = 2_000_000.0
MAXIMUM_UNIVERSE = 150
STRESS_ONE_WAY_COST = 0.0012
REGIMES = ("unfiltered", "btc_trend", "btc_breadth")
VARIANTS = ("top_decile", "top3", "single")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Point-in-time audit of published 30-day cross-sectional winner "
            "momentum held for seven days, including S0 adaptations."
        )
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--audit-data", type=Path, default=DEFAULT_AUDIT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def build_weekly_panel(daily: pd.DataFrame) -> pd.DataFrame:
    frame = daily.sort_values(["symbol", "day"]).copy()
    grouped = frame.groupby("symbol", sort=False)
    frame["formation_return"] = grouped.close.pct_change(FORMATION_DAYS)
    frame["sma_100"] = grouped.close.transform(
        lambda value: value.rolling(100, min_periods=100).mean()
    )
    frame["forward_close"] = grouped.close.shift(-HOLD_DAYS)
    frame["forward_day"] = grouped.day.shift(-HOLD_DAYS)
    frame["forward_return"] = frame.forward_close / frame.close - 1.0
    contiguous_formation = frame.day.sub(grouped.day.shift(FORMATION_DAYS)).eq(
        pd.Timedelta(days=FORMATION_DAYS)
    )
    contiguous_forward = frame.forward_day.sub(frame.day).eq(
        pd.Timedelta(days=HOLD_DAYS)
    )
    frame.loc[~contiguous_formation, "formation_return"] = np.nan
    frame.loc[~contiguous_forward, "forward_return"] = np.nan
    # Sunday close decisions create non-overlapping seven-day holding periods.
    weekly = frame.loc[frame.day.dt.dayofweek.eq(6)].copy()
    eligible = weekly.loc[
        ~weekly.symbol.isin({"BTCUSDT", "ETHUSDT"})
        & weekly.symbol_age_days.ge(MINIMUM_AGE_DAYS)
        & weekly.median_volume_30d.ge(MINIMUM_MEDIAN_VOLUME)
        & weekly.formation_return.notna()
        & weekly.forward_return.notna()
    ].copy()
    eligible["liquidity_rank"] = eligible.groupby("day")[
        "median_volume_30d"
    ].rank(method="first", ascending=False)
    eligible = eligible.loc[eligible.liquidity_rank.le(MAXIMUM_UNIVERSE)].copy()
    eligible["momentum_rank"] = eligible.groupby("day").formation_return.rank(
        pct=True, method="average"
    )
    breadth = eligible.groupby("day").formation_return.median()
    btc = weekly.loc[weekly.symbol.eq("BTCUSDT")].set_index("day")
    eligible["market_breadth"] = eligible.day.map(breadth)
    eligible["btc_formation_return"] = eligible.day.map(btc.formation_return)
    eligible["btc_above_sma_100"] = eligible.day.map(btc.close.gt(btc.sma_100))
    return eligible.reset_index(drop=True)


def regime_mask(frame: pd.DataFrame, regime: str) -> pd.Series:
    if regime == "unfiltered":
        return pd.Series(True, index=frame.index)
    if regime == "btc_trend":
        return frame.btc_above_sma_100.fillna(False) & frame.btc_formation_return.gt(0)
    if regime == "btc_breadth":
        return (
            frame.btc_above_sma_100.fillna(False)
            & frame.btc_formation_return.gt(0)
            & frame.market_breadth.gt(0)
        )
    raise ValueError(f"Unknown regime: {regime}")


def select_positions(frame: pd.DataFrame, variant: str, regime: str) -> pd.DataFrame:
    eligible = frame.loc[regime_mask(frame, regime)].copy()
    if variant == "top_decile":
        selected = eligible.loc[eligible.momentum_rank.ge(0.9)].copy()
        selected["raw_weight"] = np.sqrt(selected.median_volume_30d)
        selected["position"] = selected.raw_weight / selected.groupby("day")[
            "raw_weight"
        ].transform("sum")
    elif variant in {"top3", "single"}:
        count = 1 if variant == "single" else 3
        selected = (
            eligible.sort_values(
                ["day", "momentum_rank", "median_volume_30d"],
                ascending=[True, False, False],
            )
            .groupby("day", sort=False)
            .head(count)
            .copy()
        )
        selected["position"] = 1.0 / selected.groupby("day").symbol.transform(
            "count"
        )
    else:
        raise ValueError(f"Unknown variant: {variant}")
    return selected


def strategy_returns(
    weekly: pd.DataFrame,
    variant: str,
    regime: str,
    one_way_cost: float,
) -> pd.DataFrame:
    selected = select_positions(weekly, variant, regime)
    all_days = pd.Index(sorted(weekly.day.unique()), name="day")
    if selected.empty:
        return pd.DataFrame(
            {
                "day": all_days,
                "gross_return": 0.0,
                "turnover": 0.0,
                "net_return": 0.0,
                "positions": 0,
            }
        )
    positions = selected.pivot(index="day", columns="symbol", values="position")
    positions = positions.reindex(all_days, fill_value=0.0).fillna(0.0)
    returns = selected.pivot(index="day", columns="symbol", values="forward_return")
    returns = returns.reindex(index=all_days, columns=positions.columns).fillna(0.0)
    turnover = positions.diff().abs().sum(axis=1)
    if len(turnover):
        turnover.iloc[0] = positions.iloc[0].abs().sum()
    gross = (positions * returns).sum(axis=1)
    return pd.DataFrame(
        {
            "day": all_days,
            "gross_return": gross.values,
            "turnover": turnover.values,
            "net_return": (gross - turnover * one_way_cost).values,
            "positions": positions.ne(0).sum(axis=1).values,
        }
    )


def weekly_metrics(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {"weeks": 0, "total_return_pct": 0.0, "profit_factor": 0.0}
    returns = frame.net_return.fillna(0.0)
    equity = (1.0 + returns).cumprod()
    drawdown = equity / equity.cummax() - 1.0
    volatility = float(returns.std())
    winners = returns.clip(lower=0).sum()
    losers = -returns.clip(upper=0).sum()
    return {
        "weeks": int(len(frame)),
        "active_weeks": int(frame.positions.gt(0).sum()),
        "total_return_pct": round(float((equity.iloc[-1] - 1.0) * 100), 6),
        "annualized_return_pct": round(
            float((equity.iloc[-1] ** (52 / len(frame)) - 1.0) * 100), 6
        ),
        "annualized_volatility_pct": round(volatility * math.sqrt(52) * 100, 6),
        "sharpe": round(float(returns.mean() / volatility * math.sqrt(52)), 6)
        if volatility > 0
        else 0.0,
        "max_drawdown_pct": round(float(drawdown.min() * 100), 6),
        "turnover": round(float(frame.turnover.sum()), 6),
        "win_rate_pct": round(
            float(returns.loc[frame.positions.gt(0)].gt(0).mean() * 100), 6
        )
        if frame.positions.gt(0).any()
        else 0.0,
        "profit_factor": round(float(winners / losers), 6)
        if losers
        else (999.0 if winners else 0.0),
        "cost_sum_pct": round(
            float((frame.gross_return - frame.net_return).sum() * 100), 6
        ),
    }


def evaluate(frame: pd.DataFrame) -> dict[str, Any]:
    windows = {
        "development_2024h2": ("2024-07-01", "2025-01-01"),
        "validation_2025h1": ("2025-01-01", "2025-07-01"),
        "test_2025h2": ("2025-07-01", "2026-01-01"),
        "blind_2026h1": ("2026-01-01", "2026-07-01"),
    }
    return {
        name: weekly_metrics(frame.loc[frame.day.ge(start) & frame.day.lt(end)])
        for name, (start, end) in windows.items()
    }


def candidate_passes(report: dict[str, Any]) -> bool:
    return all(
        item.get("active_weeks", 0) >= 8
        and item.get("total_return_pct", 0.0) > 0
        and item.get("profit_factor", 0.0) > 1.0
        for item in report.values()
    )


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    panel, source = load_daily_panel([args.data, args.audit_data])
    weekly = build_weekly_panel(panel)
    results: dict[str, Any] = {}
    for regime in REGIMES:
        for variant in VARIANTS:
            for cost_name, cost in (
                ("base", ONE_WAY_COST),
                ("stress", STRESS_ONE_WAY_COST),
            ):
                name = f"{regime}_{variant}_{cost_name}"
                returns = strategy_returns(weekly, variant, regime, cost)
                report = evaluate(returns)
                results[name] = {
                    "one_way_cost": cost,
                    "windows": report,
                    "passes": candidate_passes(report),
                }
                returns.to_parquet(args.output / f"{name}.parquet", index=False)
    accepted_variants = [
        f"{regime}_{variant}"
        for regime in REGIMES
        for variant in VARIANTS
        if results[f"{regime}_{variant}_base"]["passes"]
        and results[f"{regime}_{variant}_stress"]["passes"]
    ]
    payload = {
        "method": "30-day winner momentum held seven days, long only",
        "formation_days": FORMATION_DAYS,
        "holding_days": HOLD_DAYS,
        "source": source,
        "weekly_rows": int(len(weekly)),
        "weekly_dates": int(weekly.day.nunique()),
        "results": results,
        "accepted_variants": accepted_variants,
        "accepted": bool(accepted_variants),
    }
    (args.output / "report.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
