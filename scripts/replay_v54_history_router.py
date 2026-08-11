"""Summarize the V5.4 historical-route counterfactual from Bitdata lineage.

The script uses realized net PnL already recorded by Binance, including the
captured entry/exit commissions and funding. It does not model new fills or
claim that the selected rows could all have been held concurrently.
"""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Iterable
from pathlib import Path


CORE_ROUTES = (
    "broad_down_short_pullback",
    "quiet_long_pullback",
    "mixed_long_pullback",
)


def route(row: sqlite3.Row) -> str:
    regime = str(row["market_regime"] or "").lower()
    direction = str(row["direction"] or "").upper()
    setup = str(row["setup_type"] or "").lower()
    if regime == "broad_down" and direction == "SHORT" and setup == "pullback":
        return "broad_down_short_pullback"
    if regime == "quiet" and direction == "LONG" and setup == "pullback":
        return "quiet_long_pullback"
    if regime == "mixed" and direction == "LONG" and setup == "pullback":
        return "mixed_long_pullback"
    return "other"


def summarize(rows: Iterable[sqlite3.Row]) -> dict[str, float | int | None]:
    values = list(rows)
    net_values = [float(row["net_pnl"] or 0.0) for row in values]
    costs = [
        abs(float(row["entry_commission"] or 0.0))
        + abs(float(row["close_commission"] or 0.0))
        + abs(float(row["funding_fee"] or 0.0))
        for row in values
    ]
    wins = sum(value > 0.0 for value in net_values)
    gains = sum(max(value, 0.0) for value in net_values)
    losses = -sum(min(value, 0.0) for value in net_values)
    return {
        "trades": len(values),
        "win_rate_pct": round(wins / len(values) * 100.0, 2) if values else 0.0,
        "net_pnl": round(sum(net_values), 6),
        "recorded_cost": round(sum(costs), 6),
        "profit_factor": round(gains / losses, 4) if losses else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("data/bitdata.db"))
    parser.add_argument("--holdout-version", default="v5.3")
    args = parser.parse_args()

    connection = sqlite3.connect(args.db)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT strategy_version, direction, setup_type, market_regime, net_pnl,
               realized_pnl, entry_commission, close_commission, funding_fee,
               risk_pct, hold_seconds, close_fill_time
        FROM opportunity_lineage
        WHERE net_pnl IS NOT NULL AND strategy_version IS NOT NULL
        """
    ).fetchall()
    holdout = [row for row in rows if row["strategy_version"] == args.holdout_version]
    prior = [row for row in rows if row["strategy_version"] != args.holdout_version]

    print(f"holdout={args.holdout_version} all={summarize(holdout)}")
    for name in CORE_ROUTES:
        print(f"holdout {name}={summarize(row for row in holdout if route(row) == name)}")
        print(f"prior {name}={summarize(row for row in prior if route(row) == name)}")
    print(
        "all_history_core="
        f"{summarize(row for row in rows if route(row) in CORE_ROUTES)}"
    )


if __name__ == "__main__":
    main()
