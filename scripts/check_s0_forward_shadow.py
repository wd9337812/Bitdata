from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


FAMILIES = (
    ("adaptive_30d_momentum", None),
    ("cross_sectional_momentum", "s0_xmom_24h_v2"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate forward research shadows against the S0 promotion gate "
            "(closed count, symbols, stress PF, top-3 removed, block bootstrap)."
        )
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--stress-cost-pct", type=float, default=0.60)
    parser.add_argument("--min-closed", type=int, default=30)
    parser.add_argument("--min-symbols", type=int, default=8)
    parser.add_argument("--min-pf", type=float, default=1.20)
    parser.add_argument("--bootstrap-samples", type=int, default=2000)
    return parser.parse_args()


def load_trades(connection: sqlite3.Connection, family: str, version: str | None) -> pd.DataFrame:
    if version:
        frame = pd.read_sql_query(
            "SELECT * FROM shadow_trades WHERE strategy_family=? AND strategy_version=?",
            connection,
            params=(family, version),
        )
    else:
        frame = pd.read_sql_query(
            "SELECT * FROM shadow_trades WHERE strategy_family=?",
            connection,
            params=(family,),
        )
    frame["closed_at"] = pd.to_datetime(frame.closed_at, utc=True, errors="coerce")
    frame["gross_pnl"] = pd.to_numeric(frame.gross_pnl, errors="coerce")
    frame["net_pnl"] = pd.to_numeric(frame.net_pnl, errors="coerce")
    frame["estimated_cost"] = pd.to_numeric(frame.estimated_cost, errors="coerce")
    frame["notional"] = pd.to_numeric(frame.notional, errors="coerce")
    return frame


def stress_net(frame: pd.DataFrame, stress_cost_pct: float) -> pd.Series:
    gross = frame.gross_pnl.fillna(frame.net_pnl + frame.estimated_cost)
    return gross - frame.notional * stress_cost_pct / 100.0


def pf(values: pd.Series) -> float:
    wins = values[values > 0].sum()
    losses = -values[values < 0].sum()
    if losses > 0:
        return float(wins / losses)
    return 999.0 if wins > 0 else 0.0


def block_bootstrap(
    values: pd.Series,
    weeks: pd.Series,
    samples: int,
) -> float | None:
    if len(values) < 10:
        return None
    unique_weeks = sorted(weeks.dropna().unique())
    if len(unique_weeks) < 10:
        return None
    week_values = {
        week: float(values.loc[weeks == week].sum())
        for week in unique_weeks
    }
    keys = list(week_values.keys())
    rng = np.random.default_rng(42)
    positive = 0
    for _ in range(samples):
        chosen = rng.choice(keys, size=len(keys), replace=True)
        total = sum(week_values[key] for key in chosen)
        if total > 0:
            positive += 1
    return positive / samples


def evaluate(
    connection: sqlite3.Connection,
    family: str,
    version: str | None,
    stress_cost_pct: float,
    min_closed: int,
    min_symbols: int,
    min_pf: float,
    bootstrap_samples: int,
) -> dict[str, Any]:
    frame = load_trades(connection, family, version)
    closed = frame[frame.status.eq("CLOSED")].copy()
    result: dict[str, Any] = {
        "family": family,
        "version": version or "all",
        "total": int(len(frame)),
        "closed": int(len(closed)),
        "open": int(len(frame) - len(closed)),
    }
    if closed.empty:
        result["qualified"] = False
        result["reason"] = "no closed trades"
        return result
    closed["stress_net"] = stress_net(closed, stress_cost_pct)
    result["symbols"] = int(closed.symbol.nunique())
    result["market_regimes"] = int(closed.market_regime.nunique())
    result["stress_pf"] = round(pf(closed.stress_net), 4)
    result["stress_net_usdt"] = round(float(closed.stress_net.sum()), 4)
    top3 = (
        closed.groupby("symbol")["stress_net"]
        .sum()
        .nlargest(3)
        .index
    )
    without_top3 = closed[~closed.symbol.isin(top3)]
    result["without_top3_pf"] = round(pf(without_top3.stress_net), 4)
    result["without_top3_net_usdt"] = round(
        float(without_top3.stress_net.sum()), 4
    )
    if "blind" in closed.columns:
        blind = closed[closed.blind.eq(1)]
        result["blind_trades"] = int(len(blind))
        result["blind_pf"] = round(pf(blind.stress_net), 4) if not blind.empty else None
    else:
        result["blind_trades"] = None
        result["blind_pf"] = None
    bootstrap_value = (
        block_bootstrap(
            closed.stress_net,
            closed.closed_at.dt.isocalendar().week,
            bootstrap_samples,
        )
        if not closed.stress_net.isna().all()
        else None
    )
    result["bootstrap_positive_probability"] = (
        round(bootstrap_value, 4) if bootstrap_value is not None else None
    )
    checks = {
        "closed>=30": result["closed"] >= min_closed,
        "symbols>=8": result["symbols"] >= min_symbols,
        "stress_pf>1.2": result["stress_pf"] > min_pf,
        "without_top3_pf>1": result["without_top3_pf"] > 1.0,
        "without_top3_net>0": result["without_top3_net_usdt"] > 0,
        "bootstrap>=0.95": (
            result["bootstrap_positive_probability"] is not None
            and result["bootstrap_positive_probability"] >= 0.95
        ),
    }
    result["checks"] = checks
    result["qualified"] = bool(all(checks.values()))
    result["reason"] = "all checks passed" if result["qualified"] else "not qualified"
    return result


def main() -> None:
    args = parse_args()
    connection = sqlite3.connect(args.db)
    report = {
        "stress_cost_pct": args.stress_cost_pct,
        "evaluations": [
            evaluate(
                connection,
                family,
                version,
                args.stress_cost_pct,
                args.min_closed,
                args.min_symbols,
                args.min_pf,
                args.bootstrap_samples,
            )
            for family, version in FAMILIES
        ],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    connection.close()


if __name__ == "__main__":
    main()
