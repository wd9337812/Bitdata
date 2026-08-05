from __future__ import annotations

import sqlite3

from scripts.check_s0_forward_shadow import evaluate


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE shadow_trades (
            strategy_family TEXT, strategy_version TEXT, symbol TEXT,
            status TEXT, market_regime TEXT, closed_at TEXT,
            gross_pnl REAL, estimated_cost REAL, net_pnl REAL, notional REAL
        )
        """
    )
    rows = []
    for index in range(35):
        symbol = f"SYM{index % 8}"
        gross = 2.0 if index % 3 == 0 else -1.0
        rows.append(
            (
                "adaptive_30d_momentum",
                "s0_xmom_30d_paper_v2",
                symbol,
                "CLOSED",
                "trend" if index % 2 == 0 else "range",
                f"2026-06-{(index % 28) + 1:02d}T12:00:00+00:00",
                gross,
                0.1,
                gross - 0.1,
                20.0,
            )
        )
    connection.executemany(
        "INSERT INTO shadow_trades VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    connection.commit()
    return connection


def test_evaluate_counts_and_checks() -> None:
    connection = _connection()
    result = evaluate(
        connection,
        "adaptive_30d_momentum",
        "s0_xmom_30d_paper_v2",
        stress_cost_pct=0.60,
        min_closed=30,
        min_symbols=8,
        min_pf=1.20,
        bootstrap_samples=200,
    )
    connection.close()
    assert result["closed"] == 35
    assert result["symbols"] == 8
    assert result["checks"]["closed>=30"] is True
    assert result["checks"]["symbols>=8"] is True
    assert "qualified" in result
