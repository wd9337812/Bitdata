from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRADES = ROOT / "data" / "research" / "s0_30d_momentum_adaptive" / "funded_trades_stress.parquet"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_30d_momentum_adaptive" / "risk_tier_audit.json"


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


def build_tier_report(
    trades: pd.DataFrame,
    *,
    start_equity: float = 15.0,
    hard_stop: float = 5.0,
) -> dict[str, Any]:
    ordered = trades.sort_values(["entry_ms", "exit_ms"]).copy()
    ordered["year"] = pd.to_datetime(ordered["entry_ms"], unit="ms", utc=True).dt.year
    full = simulate_tiered(ordered["net_pct"], start_equity=start_equity, hard_stop=hard_stop)
    flat20 = simulate_tiered(
        ordered["net_pct"],
        start_equity=start_equity,
        hard_stop=hard_stop,
        tier_equity=float("inf"),
    )
    by_year = {
        str(int(year)): simulate_tiered(
            rows["net_pct"],
            start_equity=start_equity,
            hard_stop=hard_stop,
        )
        for year, rows in ordered.groupby("year")
    }
    promoted = bool(
        not full["hard_stop_hit"]
        and full["final_equity"] > flat20["final_equity"]
        and all(not item["hard_stop_hit"] for item in by_year.values())
    )
    return {
        "experiment": "s0_altcoin_30d_risk_tier_20_22",
        "frozen_before_evaluation": {
            "trades_source": "funded_trades_stress.parquet",
            "cost_pct": 0.60,
            "base_risk_pct": 20.0,
            "tier_equity": 30.0,
            "tier2_risk_pct": 22.0,
            "hard_stop": hard_stop,
            "start_equity": start_equity,
            "reference_stop_pct": 15.0,
        },
        "trades": int(len(ordered)),
        "symbols": int(ordered["symbol"].nunique()),
        "profit_factor": round(profit_factor(ordered["net_pct"]), 6),
        "net_pct_points": round(float(ordered["net_pct"].sum()), 6),
        "flat20": flat20,
        "tiered_20_22": full,
        "by_year": by_year,
        "promoted": promoted,
        "warning": "Historical qualification is not live-trading approval.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trades", type=Path, default=DEFAULT_TRADES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = build_tier_report(pd.read_parquet(args.trades))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
