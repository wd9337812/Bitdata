from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.telemetry import connect  # noqa: E402

BASELINE = {
    "trades": 85,
    "long_trades": 45,
    "short_trades": 40,
    "profit_factor": 1.802188,
    "net_pct_points": 338.846743,
    "final_equity": 177.51,
    "max_drawdown_pct": 64.57,
    "hard_stop_hit": False,
}


def profit_factor(values: Iterable[float]) -> float:
    rows = [float(value) for value in values]
    gains = sum(value for value in rows if value > 0)
    losses = -sum(value for value in rows if value < 0)
    return gains / losses if losses > 0 else (999.0 if gains > 0 else 0.0)


def main() -> None:
    with connect() as conn:
        shadow_rows = conn.execute(
            """
            SELECT strategy_version, direction, net_pnl, notional, status
            FROM shadow_trades
            WHERE strategy_family = 'adaptive_30d_momentum'
            ORDER BY id
            """
        ).fetchall()
        equity_rows = conn.execute(
            """
            SELECT ts, equity, unrealized_pnl
            FROM equity_snapshots
            ORDER BY ts DESC
            LIMIT 1
            """
        ).fetchall()
    paper = [
        {
            "version": str(row["strategy_version"]),
            "direction": str(row["direction"]),
            "net_pnl": float(row["net_pnl"] or 0),
            "notional": float(row["notional"] or 0),
        }
        for row in shadow_rows
        if str(row["status"] or "") == "CLOSED"
    ]
    paper_pct = [
        item["net_pnl"] / max(item["notional"], 1e-9) * 100 for item in paper
    ]
    by_direction: dict[str, dict[str, Any]] = {}
    for direction in ("LONG", "SHORT"):
        rows = [item for item in paper if item["direction"] == direction]
        values = [item["net_pnl"] / max(item["notional"], 1e-9) * 100 for item in rows]
        by_direction[direction] = {
            "closed": len(rows),
            "wins": sum(1 for value in values if value > 0),
            "profit_factor": round(profit_factor(values), 6),
            "net_pnl": round(sum(item["net_pnl"] for item in rows), 6),
        }
    report = {
        "as_of": str(equity_rows[0]["ts"]) if equity_rows else None,
        "equity": round(float(equity_rows[0]["equity"]), 6) if equity_rows else None,
        "unrealized_pnl": (
            round(float(equity_rows[0]["unrealized_pnl"]), 6)
            if equity_rows
            else None
        ),
        "paper_30d_momentum": {
            "closed": len(paper),
            "wins": sum(1 for value in paper_pct if value > 0),
            "profit_factor": round(profit_factor(paper_pct), 6),
            "net_pnl": round(sum(item["net_pnl"] for item in paper), 6),
            "by_direction": by_direction,
        },
        "baseline_reference": BASELINE,
        "note": (
            "Baseline is the historical replay of the exact live config "
            "(85 trades, PF 1.80, 177.51U). Paper stats are the independent "
            "30-day momentum shadow; live execution is tracked by equity."
        ),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
