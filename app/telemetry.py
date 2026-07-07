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
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=15000")
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS strategy_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            stage TEXT,
            mode TEXT,
            symbol TEXT,
            action TEXT,
            result_mode TEXT,
            reason TEXT,
            score REAL,
            signal TEXT,
            entry REAL,
            stop REAL,
            take_profit REAL,
            quantity REAL,
            equity REAL,
            payload TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_equity_snapshots_ts ON equity_snapshots(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_event_logs_category_ts ON event_logs(category, ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_strategy_runs_symbol_ts ON strategy_runs(symbol, ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_strategy_runs_action_ts ON strategy_runs(action, ts)")
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


def record_strategy_run(
    state: dict[str, Any],
    account: dict[str, Any],
    decision: dict[str, Any],
    result: dict[str, Any] | None = None,
) -> None:
    result = result or {}
    scan = decision.get("scan") or {}
    mode = decision.get("mode") or (scan.get("mode") or {}).get("mode")
    candidate = decision.get("candidate") or scan.get("best") or {}
    signal = decision.get("signal") or candidate.get("signal") or {}
    symbol = decision.get("symbol") or candidate.get("symbol")
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO strategy_runs (
                ts, stage, mode, symbol, action, result_mode, reason, score,
                signal, entry, stop, take_profit, quantity, equity, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_iso(),
                state.get("stage"),
                mode,
                symbol,
                decision.get("action"),
                result.get("mode"),
                decision.get("reason") or (decision.get("risk") or {}).get("reason") or result.get("message"),
                candidate.get("score"),
                signal.get("signal"),
                signal.get("last_price"),
                signal.get("stop"),
                signal.get("take_profit"),
                decision.get("quantity"),
                account.get("equity"),
                json.dumps({"decision": compact_decision(decision), "result": result}, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        conn.commit()


def compact_decision(decision: dict[str, Any]) -> dict[str, Any]:
    """Keep optimization evidence without persisting the full market universe every loop."""
    compact = dict(decision)
    def compact_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
        item = dict(candidate)
        item.pop("coarse", None)
        item["backtests"] = {
            str(day): {
                key: value
                for key, value in (summary or {}).items()
                if key in {"trades", "wins", "win_rate", "net_pct", "profit_factor"}
            }
            for day, summary in (item.get("backtests") or {}).items()
        }
        return item

    if isinstance(compact.get("candidate"), dict):
        compact["candidate"] = compact_candidate(compact["candidate"])
    scan = dict(compact.get("scan") or {})
    if scan:
        scan.pop("ranked_symbols", None)
        scan.pop("recalled_symbols", None)
        scan["symbols"] = list(scan.get("symbols") or [])[:30]
        scan["opportunity_events"] = list(scan.get("opportunity_events") or [])[:10]
        scan["candidates"] = [compact_candidate(candidate) for candidate in list(scan.get("candidates") or [])[:24]]
        if isinstance(scan.get("best"), dict):
            scan["best"] = compact_candidate(scan["best"])
        for pool_name in ["trade_pool", "observe_pool"]:
            scan[pool_name] = [
                {
                    key: item.get(key)
                    for key in ["symbol", "direction", "score", "passed", "entry_type", "decision_reason", "risk_pct"]
                }
                for item in list(scan.get(pool_name) or [])[:24]
            ]
        compact["scan"] = scan
    return compact


def maintain_telemetry(retention_days: int = 30) -> dict[str, int]:
    cutoff = datetime.now(timezone.utc).timestamp() - max(1, retention_days) * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
    with connect() as conn:
        events = conn.execute("DELETE FROM event_logs WHERE ts < ?", (cutoff_iso,)).rowcount
        snapshots = conn.execute("DELETE FROM equity_snapshots WHERE ts < ?", (cutoff_iso,)).rowcount
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        conn.commit()
    return {"events_deleted": max(0, events), "snapshots_deleted": max(0, snapshots)}


def list_equity_snapshots(limit: int = 500) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM equity_snapshots ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def list_strategy_runs(limit: int = 200) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM strategy_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    runs = []
    for row in rows:
        item = dict(row)
        try:
            item["payload"] = json.loads(item.get("payload") or "{}")
        except json.JSONDecodeError:
            item["payload"] = {}
        runs.append(item)
    return runs


def latest_strategy_payload() -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM strategy_runs ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    item = dict(row)
    try:
        item["payload"] = json.loads(item.get("payload") or "{}")
    except json.JSONDecodeError:
        item["payload"] = {}
    return item


def list_events(limit: int = 200, category: str | None = None, include_payload: bool = True) -> list[dict[str, Any]]:
    columns = "*" if include_payload else "id, ts, level, category, message"
    with connect() as conn:
        if category:
            rows = conn.execute(
                f"SELECT {columns} FROM event_logs WHERE category = ? ORDER BY id DESC LIMIT ?",
                (category, limit),
            ).fetchall()
        else:
            rows = conn.execute(f"SELECT {columns} FROM event_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    events = []
    for row in rows:
        item = dict(row)
        if include_payload:
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
