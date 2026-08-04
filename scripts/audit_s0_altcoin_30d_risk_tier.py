from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRADES = ROOT / "data" / "research" / "s0_30d_momentum_adaptive" / "funded_trades_stress.parquet"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_30d_momentum_adaptive" / "risk_tier_audit.json"
BOOTSTRAP_SAMPLES = 500


def simulate_tiered(
    net_pct_points: Iterable[float],
    *,
    start_equity: float = 15.0,
    hard_stop: float = 5.0,
    base_risk_pct: float = 20.0,
    tier_equity: float = 30.0,
    tier2_risk_pct: float = 22.0,
    reference_stop_pct: float = 15.0,
) -> dict[str, Any]:
    equity = float(start_equity)
    peak = equity
    max_drawdown = 0.0
    stopped = False
    trades = 0
    tier2_trades = 0
    for value in net_pct_points:
        risk = tier2_risk_pct if equity >= tier_equity else base_risk_pct
        equity *= 1 + float(value) / 100 * risk / reference_stop_pct
        trades += 1
        if risk >= tier2_risk_pct:
            tier2_trades += 1
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak * 100 if peak > 0 else 0)
        if equity <= hard_stop:
            stopped = True
            break
    return {
        "trades": trades,
        "tier2_trades": tier2_trades,
        "final_equity": round(equity, 8),
        "max_drawdown_pct": round(max_drawdown, 6),
        "hard_stop_hit": stopped,
    }


def profit_factor(values: Iterable[float]) -> float:
    rows = [float(value) for value in values]
    gains = sum(value for value in rows if value > 0)
    losses = -sum(value for value in rows if value < 0)
    return gains / losses if losses > 0 else (999.0 if gains > 0 else 0.0)


def bootstrap_robustness(
    ordered: pd.DataFrame,
    *,
    start_equity: float,
    hard_stop: float,
    tier_equity: float,
    tier2_risk_pct: float,
    samples: int = BOOTSTRAP_SAMPLES,
) -> dict[str, Any]:
    weekly = ordered.copy()
    weekly["week"] = (
        pd.to_datetime(weekly["entry_ms"], unit="ms", utc=True)
        .dt.tz_localize(None)
        .dt.to_period("W")
    )
    weeks = sorted(weekly["week"].unique())
    rng = random.Random(20260804)
    no_stop = 0
    positive = 0
    finals: list[float] = []
    for _ in range(samples):
        sample_weeks = [rng.choice(weeks) for _ in weeks]
        sample = pd.concat([weekly.loc[weekly.week.eq(w)] for w in sample_weeks], ignore_index=True)
        result = simulate_tiered(
            sample["net_pct"],
            start_equity=start_equity,
            hard_stop=hard_stop,
            tier_equity=tier_equity,
            tier2_risk_pct=tier2_risk_pct,
        )
        finals.append(result["final_equity"])
        if not result["hard_stop_hit"]:
            no_stop += 1
        if result["final_equity"] > start_equity:
            positive += 1
    return {
        "samples": samples,
        "no_hard_stop_probability": round(no_stop / samples, 6),
        "positive_final_probability": round(positive / samples, 6),
        "final_p05": round(float(np.percentile(finals, 5)), 4),
        "final_p50": round(float(np.percentile(finals, 50)), 4),
        "final_p95": round(float(np.percentile(finals, 95)), 4),
    }


def concentration(
    ordered: pd.DataFrame,
    *,
    top_n: int,
    start_equity: float,
    hard_stop: float,
    tier_equity: float,
    tier2_risk_pct: float,
) -> dict[str, Any]:
    top_symbols = (
        ordered.groupby("symbol")["net_pct"].sum().nlargest(top_n).index.tolist()
    )
    reduced = ordered.loc[~ordered.symbol.isin(top_symbols)]
    result = simulate_tiered(
        reduced["net_pct"],
        start_equity=start_equity,
        hard_stop=hard_stop,
        tier_equity=tier_equity,
        tier2_risk_pct=tier2_risk_pct,
    )
    return {
        "removed_symbols": top_symbols,
        "remaining_trades": int(len(reduced)),
        **result,
    }


def build_tier_report(
    trades: pd.DataFrame,
    *,
    start_equity: float = 15.0,
    hard_stop: float = 5.0,
    tier2_risk_pct: float = 22.0,
) -> dict[str, Any]:
    ordered = trades.sort_values(["entry_ms", "exit_ms"]).copy()
    ordered["year"] = pd.to_datetime(ordered["entry_ms"], unit="ms", utc=True).dt.year
    full = simulate_tiered(
        ordered["net_pct"],
        start_equity=start_equity,
        hard_stop=hard_stop,
        tier2_risk_pct=tier2_risk_pct,
    )
    flat20 = simulate_tiered(
        ordered["net_pct"],
        start_equity=start_equity,
        hard_stop=hard_stop,
        tier_equity=float("inf"),
    )
    flat20_bootstrap = bootstrap_robustness(
        ordered,
        start_equity=start_equity,
        hard_stop=hard_stop,
        tier_equity=float("inf"),
        tier2_risk_pct=20.0,
    )
    by_year = {
        str(int(year)): simulate_tiered(
            rows["net_pct"],
            start_equity=start_equity,
            hard_stop=hard_stop,
            tier2_risk_pct=tier2_risk_pct,
        )
        for year, rows in ordered.groupby("year")
    }
    robustness = bootstrap_robustness(
        ordered,
        start_equity=start_equity,
        hard_stop=hard_stop,
        tier_equity=30.0,
        tier2_risk_pct=tier2_risk_pct,
    )
    concentration_top1 = concentration(
        ordered,
        top_n=1,
        start_equity=start_equity,
        hard_stop=hard_stop,
        tier_equity=30.0,
        tier2_risk_pct=tier2_risk_pct,
    )
    concentration_top3 = concentration(
        ordered,
        top_n=3,
        start_equity=start_equity,
        hard_stop=hard_stop,
        tier_equity=30.0,
        tier2_risk_pct=tier2_risk_pct,
    )
    start_sensitivity = {
        str(start): simulate_tiered(
            ordered["net_pct"],
            start_equity=float(start),
            hard_stop=hard_stop,
            tier2_risk_pct=tier2_risk_pct,
        )
        for start in (10, 12, 15, 20, 25)
    }
    promoted = bool(
        not full["hard_stop_hit"]
        and full["final_equity"] > flat20["final_equity"]
        and all(not item["hard_stop_hit"] for item in by_year.values())
        and robustness["no_hard_stop_probability"] >= 0.90
        and robustness["positive_final_probability"] >= 0.90
        and not concentration_top1["hard_stop_hit"]
        and not concentration_top3["hard_stop_hit"]
    )
    return {
        "experiment": f"s0_altcoin_30d_risk_tier_20_{tier2_risk_pct:g}",
        "frozen_before_evaluation": {
            "trades_source": "funded_trades_stress.parquet",
            "cost_pct": 0.60,
            "base_risk_pct": 20.0,
            "tier_equity": 30.0,
            "tier2_risk_pct": tier2_risk_pct,
            "hard_stop": hard_stop,
            "start_equity": start_equity,
            "reference_stop_pct": 15.0,
        },
        "trades": int(len(ordered)),
        "symbols": int(ordered["symbol"].nunique()),
        "profit_factor": round(profit_factor(ordered["net_pct"]), 6),
        "net_pct_points": round(float(ordered["net_pct"].sum()), 6),
        "flat20": flat20,
        "flat20_bootstrap": flat20_bootstrap,
        "tiered": full,
        "by_year": by_year,
        "bootstrap": robustness,
        "concentration_top1": concentration_top1,
        "concentration_top3": concentration_top3,
        "start_equity_sensitivity": start_sensitivity,
        "promoted": promoted,
        "warning": "Historical qualification is not live-trading approval.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trades", type=Path, default=DEFAULT_TRADES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    trades = pd.read_parquet(args.trades)
    reports = {
        "tier_20_22": build_tier_report(trades, tier2_risk_pct=22.0),
        "tier_20_25": build_tier_report(trades, tier2_risk_pct=25.0),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(reports, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    for name, report in reports.items():
        print(
            f"{name}: final={report['tiered']['final_equity']}U "
            f"hard={report['tiered']['hard_stop_hit']} "
            f"bootstrap_no_stop={report['bootstrap']['no_hard_stop_probability']} "
            f"bootstrap_positive={report['bootstrap']['positive_final_probability']} "
            f"top1_hard={report['concentration_top1']['hard_stop_hit']} "
            f"top3_hard={report['concentration_top3']['hard_stop_hit']} "
            f"promoted={report['promoted']}",
            flush=True,
        )


if __name__ == "__main__":
    main()
