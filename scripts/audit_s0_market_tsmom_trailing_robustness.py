from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_s0_market_tsmom_28d import DEFAULT_DATA
from scripts.benchmark_s0_market_tsmom_consensus import consensus_state
from scripts.benchmark_s0_market_tsmom_trailing import (
    SLOW_LOOKBACK,
    Variant,
    btc_daily,
    metrics,
    simulate,
    training_score,
)
from scripts.benchmark_s0_market_tsmom_28d import build_daily_panel, market_state


DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_market_tsmom_trailing_robustness"
BOOTSTRAP_SAMPLES = 10_000
SEED = 20260803


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Walk-forward and block-bootstrap audit for market TSMOM trailing exits."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--bootstrap-samples", type=int, default=BOOTSTRAP_SAMPLES)
    return parser.parse_args()


def variants() -> list[Variant]:
    return [
        Variant(atr, multiple, stop, hold)
        for atr in (10, 14, 20)
        for multiple in (2.0, 3.0, 4.0)
        for stop in (0.10, 0.15)
        for hold in (10, 20, 30)
    ]


def annual_report(trades: pd.DataFrame, variant: Variant) -> dict[str, Any]:
    years = pd.to_datetime(trades.entry_day, utc=True).dt.year
    return {
        str(int(year)): metrics(group, stop_pct=variant.disaster_stop_pct)
        for year, group in trades.assign(year=years).groupby("year", sort=True)
    }


def report_through_year(
    trades: pd.DataFrame, variant: Variant, end_year: int
) -> dict[str, Any]:
    years = pd.to_datetime(trades.entry_day, utc=True).dt.year
    scoped = trades.loc[years.le(end_year)]
    return {
        "training": metrics(scoped, stop_pct=variant.disaster_stop_pct),
        "annual": annual_report(scoped, variant),
    }


def expanding_walk_forward(
    trades_by_variant: dict[str, pd.DataFrame], choices: list[Variant]
) -> tuple[dict[str, Any], pd.DataFrame]:
    rows: list[pd.DataFrame] = []
    report: dict[str, Any] = {}
    for test_year in (2024, 2025, 2026):
        train_end = test_year - 1
        candidates = {
            variant.name: report_through_year(
                trades_by_variant[variant.name], variant, train_end
            )
            for variant in choices
        }
        selected_name = max(candidates, key=lambda name: training_score(candidates[name]))
        selected = next(item for item in choices if item.name == selected_name)
        trades = trades_by_variant[selected_name]
        years = pd.to_datetime(trades.entry_day, utc=True).dt.year
        test = trades.loc[years.eq(test_year)].copy()
        if not test.empty:
            test["walk_forward_test_year"] = test_year
            test["walk_forward_variant"] = selected_name
            rows.append(test)
        report[str(test_year)] = {
            "training_end_year": train_end,
            "selected_variant": selected_name,
            "training": candidates[selected_name]["training"],
            "test": metrics(test, stop_pct=selected.disaster_stop_pct),
        }
    combined = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    if combined.empty:
        combined_metrics = metrics(combined)
    else:
        per_trade_stop = combined.walk_forward_variant.map(
            {item.name: item.disaster_stop_pct for item in choices}
        )
        # All candidate stops are 10% or 15%; use the actual 10%-risk leverage per trade.
        net = combined.net_return.astype(float)
        equity_returns = net * (0.10 / per_trade_stop.astype(float)).clip(upper=3.0)
        equity = (1.0 + equity_returns).cumprod()
        drawdown = equity / equity.cummax() - 1.0
        wins = float(net.clip(lower=0).sum())
        losses = float(-net.clip(upper=0).sum())
        combined_metrics = {
            "trades": int(len(combined)),
            "win_rate": round(float(net.gt(0).mean() * 100.0), 4),
            "profit_factor": round(wins / losses, 4) if losses else 999.0,
            "net_return": round(float(equity.iloc[-1] - 1.0), 6),
            "max_drawdown": round(float(drawdown.min()), 6),
        }
    return {"years": report, "combined": combined_metrics}, combined


def _compound_block_returns(
    block_returns: list[list[float]], rng: random.Random
) -> tuple[float, float]:
    sampled = [rng.choice(block_returns) for _ in block_returns]
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for block in sampled:
        for value in block:
            equity *= 1.0 + value
            peak = max(peak, equity)
            max_drawdown = min(max_drawdown, equity / peak - 1.0)
    return equity - 1.0, max_drawdown


def block_bootstrap(
    trades: pd.DataFrame,
    variant: Variant,
    sample_count: int,
) -> dict[str, Any]:
    if trades.empty:
        return {"samples": 0}
    scoped = trades.copy()
    scoped["quarter"] = pd.to_datetime(scoped.entry_day, utc=True).dt.to_period("Q")
    leverage = min(3.0, 0.10 / variant.disaster_stop_pct)
    blocks = [
        (group.net_return.astype(float) * leverage).tolist()
        for _, group in scoped.groupby("quarter", sort=True)
        if not group.empty
    ]
    rng = random.Random(SEED)
    outcomes = [_compound_block_returns(blocks, rng) for _ in range(sample_count)]
    returns = sorted(item[0] for item in outcomes)
    drawdowns = sorted(item[1] for item in outcomes)

    def percentile(values: list[float], probability: float) -> float:
        index = min(len(values) - 1, max(0, int((len(values) - 1) * probability)))
        return float(values[index])

    return {
        "samples": sample_count,
        "calendar_quarter_blocks": len(blocks),
        "probability_positive_return": round(
            sum(value > 0 for value in returns) / len(returns), 6
        ),
        "return_p05": round(percentile(returns, 0.05), 6),
        "return_median": round(percentile(returns, 0.50), 6),
        "return_p95": round(percentile(returns, 0.95), 6),
        "drawdown_p05": round(percentile(drawdowns, 0.05), 6),
        "drawdown_median": round(percentile(drawdowns, 0.50), 6),
    }


def family_stability(
    trades_by_variant: dict[str, pd.DataFrame], choices: list[Variant]
) -> dict[str, Any]:
    rows = []
    for variant in choices:
        trades = trades_by_variant[variant.name]
        years = pd.to_datetime(trades.entry_day, utc=True).dt.year
        train = metrics(
            trades.loc[years.le(2023)], stop_pct=variant.disaster_stop_pct
        )
        oos = metrics(trades.loc[years.gt(2023)], stop_pct=variant.disaster_stop_pct)
        annual = annual_report(trades, variant)
        rows.append(
            {
                "variant": variant.name,
                "train_pf": float(train["profit_factor"]),
                "oos_pf": float(oos["profit_factor"]),
                "oos_net_return": float(oos["net_return"]),
                "positive_oos_years": sum(
                    float(annual.get(str(year), {}).get("net_return", -1)) > 0
                    for year in (2024, 2025, 2026)
                ),
            }
        )
    stable = [
        row
        for row in rows
        if row["train_pf"] > 1.10
        and row["oos_pf"] > 1.10
        and row["positive_oos_years"] == 3
    ]
    return {
        "variants": len(rows),
        "stable_variants": len(stable),
        "stable_share": round(len(stable) / len(rows), 6),
        "oos_pf_median": round(
            float(pd.Series([row["oos_pf"] for row in rows]).median()), 6
        ),
        "oos_net_return_median": round(
            float(pd.Series([row["oos_net_return"] for row in rows]).median()), 6
        ),
    }


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    panel = build_daily_panel(DEFAULT_DATA)
    state = consensus_state(market_state(panel), SLOW_LOOKBACK)
    btc = btc_daily(panel)
    choices = variants()
    trades_by_variant = {
        variant.name: simulate(btc, state, variant) for variant in choices
    }
    selected = Variant(10, 3.0, 0.15, 20)
    walk_forward, walk_forward_trades = expanding_walk_forward(
        trades_by_variant, choices
    )
    bootstrap = block_bootstrap(
        trades_by_variant[selected.name], selected, max(100, args.bootstrap_samples)
    )
    stability = family_stability(trades_by_variant, choices)
    accepted = bool(
        float(walk_forward["combined"].get("profit_factor", 0)) > 1.10
        and float(walk_forward["combined"].get("net_return", 0)) > 0
        and all(
            float(item["test"].get("net_return", 0)) > 0
            for item in walk_forward["years"].values()
        )
        and float(bootstrap.get("probability_positive_return", 0)) >= 0.80
        and float(stability.get("stable_share", 0)) >= 0.50
    )
    output = {
        "experiment": "s0_market_tsmom_trailing_robustness",
        "selected_candidate": selected.name,
        "walk_forward": walk_forward,
        "quarter_block_bootstrap": bootstrap,
        "parameter_family_stability": stability,
        "accepted": accepted,
        "decision": (
            "eligible_for_realtime_shadow"
            if accepted
            else "rejected_insufficient_robustness"
        ),
        "limitations": [
            "Bootstrap estimates historical resampling uncertainty, not future certainty.",
            "Daily bars use conservative stop ordering but do not model exchange outages.",
            "The strategy has low trade frequency and cannot support a 30-day 10,000U promise.",
        ],
    }
    if not walk_forward_trades.empty:
        walk_forward_trades.to_parquet(
            args.output / "walk_forward_trades.parquet",
            index=False,
            compression="zstd",
        )
    (args.output / "report.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
