from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from app.telemetry import connect, now_iso


def ensure_shadow_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shadow_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dedupe_key TEXT NOT NULL UNIQUE,
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            signal_type TEXT,
            mode TEXT,
            status TEXT NOT NULL,
            blocked_reason TEXT,
            entry REAL NOT NULL,
            stop REAL NOT NULL,
            take_profit REAL NOT NULL,
            last_price REAL NOT NULL,
            notional REAL NOT NULL,
            gross_pnl REAL DEFAULT 0,
            estimated_cost REAL DEFAULT 0,
            net_pnl REAL DEFAULT 0,
            outcome TEXT,
            expires_at TEXT NOT NULL,
            payload TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_status ON shadow_trades(status, opened_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_symbol ON shadow_trades(symbol, opened_at)")


def _candidate_price(candidate: dict[str, Any]) -> float:
    signal = candidate.get("signal") or {}
    ticker = candidate.get("ticker") or {}
    return float(signal.get("last_price") or ticker.get("last") or 0)


def _dedupe_key(candidate: dict[str, Any], bucket_minutes: int) -> str:
    current = datetime.now(timezone.utc)
    bucket = int(current.timestamp() // max(60, bucket_minutes * 60))
    return ":".join(
        [
            str(candidate.get("mode") or "growth"),
            str(candidate.get("symbol") or "").upper(),
            str(candidate.get("direction") or "LONG").upper(),
            str(candidate.get("entry_type") or "watch"),
            str(bucket),
        ]
    )


def update_shadow_trades(candidates: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, int]:
    """Maintain paper-only trades from scan data. This function never calls Binance."""
    if not config.get("shadow_trading_enabled", True):
        return {"opened": 0, "closed": 0, "active": 0}
    now = datetime.now(timezone.utc)
    prices = {
        str(item.get("symbol") or "").upper(): _candidate_price(item)
        for item in candidates
        if _candidate_price(item) > 0
    }
    opened = 0
    closed = 0
    with connect() as conn:
        ensure_shadow_tables(conn)
        active_rows = conn.execute("SELECT * FROM shadow_trades WHERE status = 'OPEN'").fetchall()
        for row in active_rows:
            item = dict(row)
            price = prices.get(str(item["symbol"]).upper())
            if not price:
                continue
            direction = str(item["direction"]).upper()
            stop_hit = price <= float(item["stop"]) if direction == "LONG" else price >= float(item["stop"])
            take_hit = price >= float(item["take_profit"]) if direction == "LONG" else price <= float(item["take_profit"])
            expired = now >= datetime.fromisoformat(str(item["expires_at"]))
            if not (stop_hit or take_hit or expired):
                conn.execute("UPDATE shadow_trades SET last_price = ? WHERE id = ?", (price, item["id"]))
                continue
            exit_price = float(item["stop"]) if stop_hit else float(item["take_profit"]) if take_hit else price
            move = (exit_price - float(item["entry"])) / float(item["entry"])
            if direction == "SHORT":
                move = -move
            gross = float(item["notional"]) * move
            cost = float(item["estimated_cost"])
            outcome = "STOP" if stop_hit else "TAKE_PROFIT" if take_hit else "TIME_EXIT"
            conn.execute(
                """
                UPDATE shadow_trades
                SET status = 'CLOSED', closed_at = ?, last_price = ?, gross_pnl = ?, net_pnl = ?, outcome = ?
                WHERE id = ?
                """,
                (now_iso(), exit_price, gross, gross - cost, outcome, item["id"]),
            )
            closed += 1

        min_score = float(config.get("shadow_min_candidate_score", 70.0))
        bucket_minutes = int(config.get("shadow_dedupe_minutes", 10))
        hold_minutes = int(config.get("shadow_max_hold_minutes", 120))
        notional = float(config.get("shadow_reference_notional_usdt", 20.0))
        cost_pct = float(config.get("shadow_round_trip_cost_pct", 0.12)) / 100
        for candidate in candidates:
            signal = candidate.get("signal") or {}
            entry = _candidate_price(candidate)
            stop = float(signal.get("stop") or 0)
            take = float(signal.get("take_profit") or 0)
            if (
                float(candidate.get("score") or 0) < min_score
                or entry <= 0
                or stop <= 0
                or take <= 0
                or stop == take
            ):
                continue
            key = _dedupe_key(candidate, bucket_minutes)
            try:
                conn.execute(
                    """
                    INSERT INTO shadow_trades (
                        dedupe_key, opened_at, symbol, direction, signal_type, mode, status,
                        blocked_reason, entry, stop, take_profit, last_price, notional,
                        estimated_cost, expires_at, payload
                    ) VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        now_iso(),
                        str(candidate.get("symbol") or "").upper(),
                        str(candidate.get("direction") or "LONG").upper(),
                        str(candidate.get("entry_type") or "watch"),
                        str(candidate.get("mode") or "growth"),
                        str(candidate.get("decision_reason") or candidate.get("reason") or "未通过实盘条件"),
                        entry,
                        stop,
                        take,
                        entry,
                        notional,
                        notional * cost_pct,
                        (now + timedelta(minutes=hold_minutes)).isoformat(),
                        json.dumps(
                            {
                                "score": candidate.get("score"),
                                "market_state": candidate.get("market_state"),
                                "adaptive_thresholds": candidate.get("adaptive_thresholds"),
                                "cost_ratio": candidate.get("cost_ratio"),
                                "passed": candidate.get("passed"),
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    ),
                )
                opened += 1
            except sqlite3.IntegrityError:
                pass
        conn.commit()
        active = conn.execute("SELECT COUNT(*) FROM shadow_trades WHERE status = 'OPEN'").fetchone()[0]
    return {"opened": opened, "closed": closed, "active": int(active)}


def shadow_summary(limit: int = 100) -> dict[str, Any]:
    with connect() as conn:
        ensure_shadow_tables(conn)
        rows = conn.execute("SELECT * FROM shadow_trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        aggregate = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END) AS closed,
                SUM(CASE WHEN status = 'CLOSED' AND net_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN status = 'CLOSED' THEN net_pnl ELSE 0 END) AS net_pnl,
                SUM(CASE WHEN status = 'CLOSED' THEN estimated_cost ELSE 0 END) AS cost
            FROM shadow_trades
            """
        ).fetchone()
    stats = dict(aggregate) if aggregate else {}
    for key in ["total", "active", "closed", "wins", "net_pnl", "cost"]:
        stats[key] = stats.get(key) or 0
    closed = int(stats.get("closed") or 0)
    wins = int(stats.get("wins") or 0)
    stats["win_rate"] = wins / closed * 100 if closed else 0.0
    trades = []
    for row in rows:
        item = dict(row)
        try:
            item["payload"] = json.loads(item.get("payload") or "{}")
        except json.JSONDecodeError:
            item["payload"] = {}
        trades.append(item)
    return {"stats": stats, "trades": trades}
