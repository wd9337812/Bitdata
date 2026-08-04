from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

import pandas as pd


def profit_factor(values: Iterable[float]) -> float:
    rows = [float(value) for value in values]
    gains = sum(value for value in rows if value > 0)
    losses = -sum(value for value in rows if value < 0)
    return gains / losses if losses > 0 else (999.0 if gains > 0 else 0.0)


def simulate_equity(
    net_pct_points: Iterable[float],
    *,
    start_equity: float = 15.0,
    risk_pct: float = 15.0,
    reference_stop_pct: float = 15.0,
    hard_stop: float = 5.0,
) -> dict[str, Any]:
    equity = float(start_equity)
    peak = equity
    max_drawdown = 0.0
    stopped = False
    trades = 0
    for value in net_pct_points:
        equity *= 1 + float(value) / 100 * risk_pct / reference_stop_pct
        trades += 1
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak * 100 if peak > 0 else 0)
        if equity <= hard_stop:
            stopped = True
            break
    return {
        "trades": trades,
        "final_equity": round(equity, 8),
        "max_drawdown_pct": round(max_drawdown, 6),
        "hard_stop_hit": stopped,
    }


def build_report(
    trades: pd.DataFrame,
    *,
    start_equity: float = 15.0,
    hard_stop: float = 5.0,
) -> dict[str, Any]:
    ordered = trades.sort_values(["entry_ms", "exit_ms"]).copy()
    results = {
        str(risk): simulate_equity(
            ordered["net_pct"],
            start_equity=start_equity,
            risk_pct=float(risk),
            hard_stop=hard_stop,
        )
        for risk in (15, 20, 22, 25, 30)
    }
    by_year: dict[str, Any] = {}
    ordered["year"] = pd.to_datetime(ordered["entry_ms"], unit="ms", utc=True).dt.year
    for year, rows in ordered.groupby("year"):
        by_year[str(int(year))] = {
            "trades": int(len(rows)),
            "profit_factor": round(profit_factor(rows["net_pct"]), 6),
            "net_pct_points": round(float(rows["net_pct"].sum()), 6),
            **{
                f"risk{risk}": simulate_equity(
                    rows["net_pct"],
                    start_equity=start_equity,
                    risk_pct=float(risk),
                    hard_stop=hard_stop,
                )
                for risk in (15, 20, 22, 25, 30)
            },
        }
    by_direction = {
        direction: {
            "trades": int(len(rows)),
            "profit_factor": round(profit_factor(rows["net_pct"]), 6),
            "net_pct_points": round(float(rows["net_pct"].sum()), 6),
        }
        for direction, rows in ordered.groupby("direction")
    }
    return {
        "strategy": "s0_xmom_30d_live_v1",
        "source": "official_point_in_time_binance_usdm",
        "cost_pct": round(float(ordered["cost_pct"].max()), 6),
        "trades": int(len(ordered)),
        "symbols": int(ordered["symbol"].nunique()),
        "profit_factor": round(profit_factor(ordered["net_pct"]), 6),
        "net_pct_points": round(float(ordered["net_pct"].sum()), 6),
        "by_year": by_year,
        "by_direction": by_direction,
        "equity_stress": results,
        "decision": {
            "selected_risk_pct": 20.0,
            "risk20_promoted": bool(
                not results["20"]["hard_stop_hit"]
                and all(not item["risk20"]["hard_stop_hit"] for item in by_year.values())
                and results["20"]["final_equity"] > results["15"]["final_equity"]
            ),
            "risk30_rejected": bool(
                results["30"]["hard_stop_hit"]
                or any(item["risk30"]["hard_stop_hit"] for item in by_year.values())
            ),
            "risk25_rejected": bool(
                results["25"]["hard_stop_hit"]
                or any(item["risk25"]["hard_stop_hit"] for item in by_year.values())
            ),
            "live_qualified": bool(
                profit_factor(ordered["net_pct"]) > 1.25
                and all(item["net_pct_points"] > 0 for item in by_year.values())
                and all(not item["risk20"]["hard_stop_hit"] for item in by_year.values())
                and not results["20"]["hard_stop_hit"]
            ),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--trades",
        default="data/research/s0_30d_momentum_adaptive/funded_trades_stress.parquet",
    )
    parser.add_argument(
        "--output",
        default="data/research/s0_30d_momentum_adaptive/live_candidate_audit.json",
    )
    args = parser.parse_args()
    report = build_report(pd.read_parquet(args.trades))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
