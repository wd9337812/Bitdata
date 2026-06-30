from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def db_path() -> Path:
    config_path = Path(os.getenv("APP_CONFIG_PATH", "./data/config.json"))
    return config_path.with_name("bitdata.db")


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS equity_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            equity REAL,
            available_balance REAL,
            unrealized_pnl REAL,
            stage TEXT,
            mode TEXT,
            best_symbol TEXT,
            best_score REAL,
            action TEXT,
            reason TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS event_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            level TEXT NOT NULL,
            category TEXT NOT NULL,
            message TEXT NOT NULL,
            payload TEXT
        )
        """
    )
    conn.commit()
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_equity_snapshot(
    account: dict[str, Any],
    state: dict[str, Any],
    mode: str | None = None,
    best: dict[str, Any] | None = None,
    action: str | None = None,
    reason: str | None = None,
) -> None:
    best = best or {}
    ticker = best.get("ticker") or {}
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO equity_snapshots (
                ts, equity, available_balance, unrealized_pnl, stage, mode,
                best_symbol, best_score, action, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_iso(),
                account.get("equity"),
                account.get("available_balance"),
                account.get("unrealized_pnl"),
                state.get("stage"),
                mode,
                best.get("symbol") or ticker.get("symbol"),
                best.get("score"),
                action,
                reason,
            ),
        )
        conn.commit()


def record_event(level: str, category: str, message: str, payload: dict[str, Any] | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO event_logs (ts, level, category, message, payload) VALUES (?, ?, ?, ?, ?)",
            (now_iso(), level, category, message, json.dumps(payload or {}, ensure_ascii=False)),
        )
        conn.commit()


def list_equity_snapshots(limit: int = 500) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM equity_snapshots ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def list_events(limit: int = 200, category: str | None = None) -> list[dict[str, Any]]:
    with connect() as conn:
        if category:
            rows = conn.execute(
                "SELECT * FROM event_logs WHERE category = ? ORDER BY id DESC LIMIT ?",
                (category, limit),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM event_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    events = []
    for row in rows:
        item = dict(row)
        try:
            item["payload"] = json.loads(item.get("payload") or "{}")
        except json.JSONDecodeError:
            item["payload"] = {}
        events.append(item)
    return events


def heartbeat() -> dict[str, Any]:
    with connect() as conn:
        snap = conn.execute("SELECT * FROM equity_snapshots ORDER BY id DESC LIMIT 1").fetchone()
        event = conn.execute("SELECT * FROM event_logs ORDER BY id DESC LIMIT 1").fetchone()
    return {
        "last_snapshot": dict(snap) if snap else None,
        "last_event": dict(event) if event else None,
    }
