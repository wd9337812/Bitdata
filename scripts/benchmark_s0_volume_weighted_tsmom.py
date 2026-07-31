from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_s0_donchian_ensemble import (  # noqa: E402
    ONE_WAY_COST,
    daily_symbol_frame,
    return_metrics,
)
from scripts.benchmark_s0_xmom_point_in_time import load_manifest  # noqa: E402


DEFAULT_DATA = ROOT / "data" / "research" / "binance_um_point_in_time_1h_2024_2025"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_volume_weighted_tsmom"
FORMATION_DAYS = (1, 7, 14)
MINIMUM_AGE_DAYS = 30
MINIMUM_DAILY_QUOTE_VOLUME = 1_000_000.0
STRESS_ONE_WAY_COST = 0.0012
STABLE_BASES = {
    "BUSD",
    "DAI",
    "FDUSD",
    "TUSD",
    "USDC",
    "USDE",
    "USDP",
    "USDS",
    "USDT",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Reproduce volume-weighted winner-minus-loser crypto momentum and "
            "audit point-in-time S0 adaptations after realistic turnover costs."
        )
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def base_asset(symbol: str) -> str:
    return symbol.removesuffix("USDT")


def is_stablecoin_contract(symbol: str) -> bool:
    return base_asset(symbol) in STABLE_BASES


def build_daily_panel(data: Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    starts, manifest = load_manifest(data)
    frames: list[pd.DataFrame] = []
    for symbol in sorted(starts):
        if is_stablecoin_contract(symbol):
            continue
        path = data / "parquet" / f"{symbol}.parquet"
        if not path.exists():
            continue
        frame = daily_symbol_frame(path, starts[symbol])[
            [
                "symbol",
                "day",
                "close",
                "quote_volume",
                "next_return",
                "symbol_age_days",
            ]
        ].copy()
        next_day_is_contiguous = frame.day.shift(-1).sub(frame.day).eq(
            pd.Timedelta(days=1)
        )
        frame.loc[~next_day_is_contiguous, "next_return"] = np.nan
        frame["median_quote_volume_30d"] = frame.quote_volume.rolling(
            30, min_periods=20
        ).median()
        for lookback in FORMATION_DAYS:
            frame[f"formation_{lookback}d"] = frame.close.pct_change(lookback)
            formation_is_contiguous = frame.day.sub(frame.day.shift(lookback)).eq(
                pd.Timedelta(days=lookback)
            )
            frame.loc[
                ~formation_is_contiguous, f"formation_{lookback}d"
            ] = np.nan
        frames.append(frame)
    panel = pd.concat(frames, ignore_index=True)
    return panel.sort_values(["day", "symbol"]).reset_index(drop=True), manifest


def eligible_universe(panel: pd.DataFrame, top_n: int | None = None) -> pd.DataFrame:
    eligible = panel.loc[
        panel.symbol_age_days.ge(MINIMUM_AGE_DAYS)
        & panel.quote_volume.ge(MINIMUM_DAILY_QUOTE_VOLUME)
        & panel.next_return.notna()
    ].copy()
    if top_n is not None:
        eligible["liquidity_rank"] = eligible.groupby("day")[
            "median_quote_volume_30d"
        ].rank(method="first", ascending=False)
        eligible = eligible.loc[eligible.liquidity_rank.le(top_n)].copy()
    return eligible


def volume_side_weights(frame: pd.DataFrame, lookback: int) -> pd.Series:
    formation = frame[f"formation_{lookback}d"]
    weights = pd.Series(0.0, index=frame.index)
    winners = formation.gt(0)
    losers = formation.lt(0)
    winner_volume = frame.loc[winners, "quote_volume"].sum()
    loser_volume = frame.loc[losers, "quote_volume"].sum()
    if winner_volume > 0:
        weights.loc[winners] = (
            frame.loc[winners, "quote_volume"] / winner_volume
        )
    if loser_volume > 0:
        weights.loc[losers] = -(
            frame.loc[losers, "quote_volume"] / loser_volume
        )
    return weights


def weighted_portfolio_daily(
    panel: pd.DataFrame,
    lookback: int,
    one_way_cost: float,
    top_n: int | None = None,
    gross_exposure: float = 2.0,
) -> pd.DataFrame:
    frame = eligible_universe(panel, top_n)
    formation_column = f"formation_{lookback}d"
    frame = frame.loc[frame[formation_column].notna()].copy()
    raw = frame.groupby("day", group_keys=False).apply(
        volume_side_weights,
        lookback=lookback,
        include_groups=False,
    )
    # The paper is +1 winner / -1 loser (gross 2). Gross-one is separately
    # reported as the directly investable, unlevered adaptation.
    frame["position"] = raw.reindex(frame.index).fillna(0.0) * (gross_exposure / 2.0)
    all_days = pd.Index(sorted(panel.day.unique()), name="day")
    positions = frame.pivot(index="day", columns="symbol", values="position").reindex(
        all_days, fill_value=0.0
    ).fillna(0.0)
    forward = frame.pivot(index="day", columns="symbol", values="next_return").reindex(
        index=all_days, columns=positions.columns
    ).fillna(0.0)
    turnover = positions.diff().abs().sum(axis=1)
    if len(turnover):
        turnover.iloc[0] = positions.iloc[0].abs().sum()
    gross = (positions * forward).sum(axis=1)
    daily = pd.DataFrame(
        {
            "day": all_days,
            "gross_return": gross.values,
            "turnover": turnover.values,
            "net_return": (gross - turnover * one_way_cost).values,
            "long_count": positions.gt(0).sum(axis=1).values,
            "short_count": positions.lt(0).sum(axis=1).values,
        }
    )
    return daily


def top_k_daily(
    panel: pd.DataFrame,
    lookback: int,
    k: int,
    one_way_cost: float,
) -> pd.DataFrame:
    frame = eligible_universe(panel)
    formation_column = f"formation_{lookback}d"
    frame = frame.loc[frame[formation_column].notna()].copy()
    frame["direction"] = np.sign(frame[formation_column])
    frame = frame.loc[frame.direction.ne(0)].copy()
    selected = (
        frame.sort_values(
            ["day", "quote_volume", "symbol"], ascending=[True, False, True]
        )
        .groupby("day", sort=False)
        .head(k)
        .copy()
    )
    selected["position"] = selected.direction / selected.groupby("day")[
        "symbol"
    ].transform("count")
    all_days = pd.Index(sorted(panel.day.unique()), name="day")
    positions = selected.pivot(
        index="day", columns="symbol", values="position"
    ).reindex(all_days, fill_value=0.0).fillna(0.0)
    forward = selected.pivot(
        index="day", columns="symbol", values="next_return"
    ).reindex(index=all_days, columns=positions.columns).fillna(0.0)
    turnover = positions.diff().abs().sum(axis=1)
    if len(turnover):
        turnover.iloc[0] = positions.iloc[0].abs().sum()
    gross = (positions * forward).sum(axis=1)
    active = positions.abs().sum(axis=1).gt(0)
    direction = np.sign(positions.sum(axis=1)) if k == 1 else pd.Series(0, index=all_days)
    symbol = (
        positions.abs().idxmax(axis=1).where(active)
        if k == 1
        else pd.Series(None, index=all_days)
    )
    return pd.DataFrame(
        {
            "day": all_days,
            "symbol": symbol.values,
            "direction": np.asarray(direction),
            "gross_return": gross.values,
            "turnover": turnover.values,
            "net_return": (gross - turnover * one_way_cost).values,
        }
    )


def sparse_balanced_daily(
    panel: pd.DataFrame,
    lookback: int,
    per_side: int,
    one_way_cost: float,
) -> pd.DataFrame:
    frame = eligible_universe(panel)
    formation_column = f"formation_{lookback}d"
    frame = frame.loc[frame[formation_column].notna()].copy()
    frame["direction"] = np.sign(frame[formation_column])
    frame = frame.loc[frame.direction.ne(0)].copy()
    selected = (
        frame.sort_values(
            ["day", "direction", "quote_volume", "symbol"],
            ascending=[True, True, False, True],
        )
        .groupby(["day", "direction"], sort=False)
        .head(per_side)
        .copy()
    )
    side_count = selected.groupby(["day", "direction"])["symbol"].transform(
        "count"
    )
    selected["position"] = selected.direction * 0.5 / side_count
    all_days = pd.Index(sorted(panel.day.unique()), name="day")
    positions = selected.pivot(
        index="day", columns="symbol", values="position"
    ).reindex(all_days, fill_value=0.0).fillna(0.0)
    forward = selected.pivot(
        index="day", columns="symbol", values="next_return"
    ).reindex(index=all_days, columns=positions.columns).fillna(0.0)
    turnover = positions.diff().abs().sum(axis=1)
    if len(turnover):
        turnover.iloc[0] = positions.iloc[0].abs().sum()
    gross = (positions * forward).sum(axis=1)
    return pd.DataFrame(
        {
            "day": all_days,
            "gross_return": gross.values,
            "turnover": turnover.values,
            "net_return": (gross - turnover * one_way_cost).values,
            "long_count": positions.gt(0).sum(axis=1).values,
            "short_count": positions.lt(0).sum(axis=1).values,
        }
    )


def daily_metrics(frame: pd.DataFrame) -> dict[str, Any]:
    metrics = return_metrics(frame)
    winners = frame.net_return.clip(lower=0).sum()
    losers = -frame.net_return.clip(upper=0).sum()
    gross_profit = frame.gross_return.clip(lower=0).sum()
    cost = frame.turnover.sum() * (
        (frame.gross_return - frame.net_return).sum() / frame.turnover.sum()
        if frame.turnover.sum()
        else 0.0
    )
    metrics.update(
        {
            "positive_days_pct": round(float(frame.net_return.gt(0).mean() * 100), 6),
            "daily_profit_factor": round(float(winners / losers), 6)
            if losers
            else (999.0 if winners else 0.0),
            "gross_positive_return_sum_pct": round(float(gross_profit * 100), 6),
            "cost_sum_pct": round(float(cost * 100), 6),
        }
    )
    return metrics


def evaluate(daily: pd.DataFrame) -> dict[str, Any]:
    windows = {
        "development_2024h2": ("2024-07-01", "2025-01-01"),
        "validation_2025h1": ("2025-01-01", "2025-07-01"),
        "test_2025h2": ("2025-07-01", "2026-01-01"),
    }
    return {
        name: daily_metrics(daily.loc[daily.day.ge(start) & daily.day.lt(end)])
        for name, (start, end) in windows.items()
    }


def candidate_passes(report: dict[str, Any]) -> bool:
    return all(
        window["total_return_pct"] > 0
        and window["daily_profit_factor"] > 1.0
        for window in report.values()
    )


def main() -> None:
    args = parse_args()
    panel, manifest = build_daily_panel(args.data)
    candidates: dict[str, Any] = {}
    for lookback in FORMATION_DAYS:
        variants = {
            "paper_gross2_actual_turnover": weighted_portfolio_daily(
                panel, lookback, ONE_WAY_COST, gross_exposure=2.0
            ),
            "investable_gross1_actual_turnover": weighted_portfolio_daily(
                panel, lookback, ONE_WAY_COST, gross_exposure=1.0
            ),
            "top150_gross1_actual_turnover": weighted_portfolio_daily(
                panel, lookback, ONE_WAY_COST, top_n=150, gross_exposure=1.0
            ),
            "s0_top_volume_single": top_k_daily(
                panel, lookback, 1, ONE_WAY_COST
            ),
            "s0_top_volume_3": top_k_daily(panel, lookback, 3, ONE_WAY_COST),
            "s0_top_volume_5": top_k_daily(panel, lookback, 5, ONE_WAY_COST),
            "sparse_balanced_1_per_side": sparse_balanced_daily(
                panel, lookback, 1, ONE_WAY_COST
            ),
            "sparse_balanced_3_per_side": sparse_balanced_daily(
                panel, lookback, 3, ONE_WAY_COST
            ),
            "sparse_balanced_5_per_side": sparse_balanced_daily(
                panel, lookback, 5, ONE_WAY_COST
            ),
        }
        for variant, daily in variants.items():
            base = evaluate(daily)
            stressed_daily = daily.copy()
            stressed_daily["net_return"] = (
                stressed_daily.gross_return
                - stressed_daily.turnover * STRESS_ONE_WAY_COST
            )
            stress = evaluate(stressed_daily)
            candidates[f"{lookback}d_{variant}"] = {
                "base": base,
                "stress": stress,
                "qualified_base": candidate_passes(base),
                "qualified_stress": candidate_passes(stress),
            }
    qualified = [
        name
        for name, result in candidates.items()
        if result["qualified_base"] and result["qualified_stress"]
    ]
    report = {
        "experiment": "point_in_time_volume_weighted_tsmom",
        "source": manifest.get("source"),
        "research_source": "Huang (2025), Essays on cryptocurrency markets, Chapter 3",
        "method": (
            "Positive formation return = winner/long; negative = loser/short; "
            "daily quote-volume weights; next-day return; daily rebalance."
        ),
        "adaptation_caveats": [
            "Binance USD-M futures replace the paper's broad CoinMarketCap spot universe.",
            "A 30-day listing age and 1m USDT daily volume floor replace the paper's 1m USD market-cap filter.",
            "Actual point-in-time turnover costs replace the paper's fixed 10 bps per side convention.",
            "Top-k variants are separately labelled S0 adaptations and cannot inherit diversified portfolio claims.",
        ],
        "universe_symbols": int(panel.symbol.nunique()),
        "base_one_way_cost_pct": ONE_WAY_COST * 100,
        "stress_one_way_cost_pct": STRESS_ONE_WAY_COST * 100,
        "candidates": candidates,
        "qualified_candidates": qualified,
        "decision": "proceed_to_2026_and_minute_validation" if qualified else "research_only_not_eligible",
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
