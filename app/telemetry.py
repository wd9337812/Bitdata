from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_EVENT_THROTTLE: dict[str, float] = {}
_STRATEGY_RUN_THROTTLE: dict[str, float] = {}
_SCHEMA_READY: set[str] = set()
_SCHEMA_LOCK = threading.Lock()
_STORAGE_CACHE: tuple[float, dict[str, Any]] | None = None


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
    path_key = str(path.resolve())
    if path_key in _SCHEMA_READY:
        return conn
    with _SCHEMA_LOCK:
        if path_key in _SCHEMA_READY:
            return conn
        _initialize_schema(conn)
        _SCHEMA_READY.add(path_key)
    return conn


def _initialize_schema(conn: sqlite3.Connection) -> None:
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


def record_event_throttled(
    level: str,
    category: str,
    message: str,
    payload: dict[str, Any] | None = None,
    *,
    throttle_seconds: int = 60,
    key: str | None = None,
) -> bool:
    throttle_key = key or f"{level}:{category}:{message}"
    current = datetime.now(timezone.utc).timestamp()
    last = _EVENT_THROTTLE.get(throttle_key, 0.0)
    if throttle_seconds > 0 and current - last < throttle_seconds:
        return False
    _EVENT_THROTTLE[throttle_key] = current
    try:
        record_event(level, category, message, payload)
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower():
            raise
        return False
    return True


def record_strategy_run(
    state: dict[str, Any],
    account: dict[str, Any],
    decision: dict[str, Any],
    result: dict[str, Any] | None = None,
    *,
    throttle_seconds: int = 0,
) -> bool:
    result = result or {}
    scan = decision.get("scan") or {}
    mode = decision.get("mode") or (scan.get("mode") or {}).get("mode")
    candidate = decision.get("candidate") or scan.get("best") or {}
    signal = decision.get("signal") or candidate.get("signal") or {}
    symbol = decision.get("symbol") or candidate.get("symbol")
    if throttle_seconds > 0:
        throttle_key = f"{db_path()}:" + ":".join(
            str(value or "-")
            for value in [mode, decision.get("action"), decision.get("reason") or (decision.get("risk") or {}).get("reason"), symbol]
        )
        current = datetime.now(timezone.utc).timestamp()
        last = _STRATEGY_RUN_THROTTLE.get(throttle_key, 0.0)
        if current - last < throttle_seconds:
            return False
        _STRATEGY_RUN_THROTTLE[throttle_key] = current
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
    return True


def compact_decision(decision: dict[str, Any]) -> dict[str, Any]:
    """Keep optimization evidence without persisting the full market universe every loop."""
    compact = dict(decision)
    def compact_candidate(candidate: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
        keys = {
            "symbol", "direction", "mode", "strategy", "strategy_family", "strategy_version", "strategy_role",
            "opportunity_id",
            "score", "passed", "reason", "decision_reason", "entry_type", "entry_type_label",
            "symbol_pool", "cost_ratio", "estimated_cost_pct", "expected_profit_pct",
            "risk_pct", "base_risk_pct", "leverage", "margin_pct", "current_score",
            "smart_flow_score_delta",
        }
        item = {key: candidate.get(key) for key in keys if key in candidate}
        for nested_key in ("market_structure", "opportunity_v4", "market_state", "risk_adjustment", "ticker", "smart_flow"):
            if isinstance(candidate.get(nested_key), dict):
                item[nested_key] = candidate[nested_key]
        signal = candidate.get("signal") or {}
        if isinstance(signal, dict):
            item["signal"] = {
                key: signal.get(key)
                for key in (
                    "signal", "entry_type", "last_price", "stop", "take_profit", "atr", "atr_pct",
                    "volume_acceleration", "directed_trade_flow", "impulse_atr", "adverse_wick_ratio",
                    "breakout_extension_atr", "entry_phase", "protection_profile",
                )
                if key in signal
            }
        item["backtests"] = {
            str(day): {
                key: value
                for key, value in (summary or {}).items()
                if key in {"trades", "wins", "win_rate", "net_pct", "profit_factor"}
            }
            for day, summary in (candidate.get("backtests") or {}).items()
        }
        if not detail:
            return item
        return item

    if isinstance(compact.get("candidate"), dict):
        compact["candidate"] = compact_candidate(compact["candidate"], detail=True)
    raw_scan = dict(compact.get("scan") or {})
    scan = {
        key: raw_scan.get(key)
        for key in ("mode", "funnel", "market_context", "market_structure", "opportunity_v4", "elapsed_seconds")
        if key in raw_scan
    }
    if isinstance(raw_scan.get("best"), dict):
        scan["best"] = compact_candidate(raw_scan["best"], detail=True)
    if raw_scan:
        scan["symbols"] = list(raw_scan.get("symbols") or [])[:20]
        scan["opportunity_events"] = list(raw_scan.get("opportunity_events") or [])[:6]
        scan["candidates"] = [compact_candidate(candidate) for candidate in list(raw_scan.get("candidates") or [])[:8]]
        for pool_name in ["trade_pool", "observe_pool"]:
            scan[pool_name] = [
                {
                    key: item.get(key)
                    for key in ["symbol", "direction", "score", "passed", "entry_type", "decision_reason", "risk_pct"]
                }
                for item in list(raw_scan.get(pool_name) or [])[:16]
            ]
        compact["scan"] = scan
    return compact


def _delete_batch(conn: sqlite3.Connection, table: str, column: str, cutoff: str, batch_size: int) -> int:
    before = conn.total_changes
    conn.execute(
        f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} WHERE {column} < ? ORDER BY id LIMIT ?)",
        (cutoff, batch_size),
    )
    return max(0, conn.total_changes - before)


def maintain_telemetry(
    retention_days: int = 30,
    *,
    strategy_run_retention_days: int = 7,
    shadow_trade_retention_days: int = 14,
    batch_size: int = 50_000,
    max_batches: int = 4,
) -> dict[str, int]:
    cutoff = datetime.now(timezone.utc).timestamp() - max(1, retention_days) * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
    strategy_cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, strategy_run_retention_days))
    shadow_cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, shadow_trade_retention_days))
    try:
        with connect() as conn:
            events = conn.execute("DELETE FROM event_logs WHERE ts < ?", (cutoff_iso,)).rowcount
            snapshots = conn.execute("DELETE FROM equity_snapshots WHERE ts < ?", (cutoff_iso,)).rowcount
            strategy_runs = 0
            for _ in range(max(1, max_batches)):
                deleted = _delete_batch(conn, "strategy_runs", "ts", strategy_cutoff.isoformat(), max(100, batch_size))
                strategy_runs += deleted
                if deleted < max(100, batch_size):
                    break
            try:
                before_shadow = conn.total_changes
                conn.execute(
                    "DELETE FROM shadow_trades WHERE id IN (SELECT id FROM shadow_trades WHERE status = 'CLOSED' AND closed_at < ? ORDER BY id LIMIT ?)",
                    (shadow_cutoff.isoformat(), max(100, batch_size)),
                )
                shadow_trades = max(0, conn.total_changes - before_shadow)
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).lower():
                    raise
                shadow_trades = 0
            conn.commit()
            try:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                conn.execute("PRAGMA optimize")
            except sqlite3.OperationalError:
                pass
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower():
            raise
        record_event_throttled(
            "warning",
            "telemetry_maintenance",
            "database table is locked",
            {},
            throttle_seconds=300,
        )
        return {"events_deleted": 0, "snapshots_deleted": 0, "strategy_runs_deleted": 0, "shadow_trades_deleted": 0}
    return {
        "events_deleted": max(0, events),
        "snapshots_deleted": max(0, snapshots),
        "strategy_runs_deleted": max(0, strategy_runs),
        "shadow_trades_deleted": max(0, shadow_trades),
    }


def telemetry_storage_status() -> dict[str, Any]:
    global _STORAGE_CACHE
    now = time.monotonic()
    if _STORAGE_CACHE and now - _STORAGE_CACHE[0] <= 60:
        return _STORAGE_CACHE[1]
    path = db_path()
    if not path.exists():
        return {"database_mb": 0.0, "wal_mb": 0.0, "reclaimable_mb": 0.0}
    with connect() as conn:
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        counts: dict[str, int] = {}
        for table in (
            "strategy_runs",
            "live_trade_records",
            "shadow_trades",
            "opportunity_lineage",
            "equity_snapshots",
            "event_logs",
        ):
            try:
                query = (
                    f"SELECT COUNT(*) FROM {table}"
                    if table == "opportunity_lineage"
                    else f"SELECT COALESCE(MAX(id), 0) FROM {table}"
                )
                counts[table] = int(conn.execute(query).fetchone()[0])
            except sqlite3.OperationalError:
                counts[table] = 0
    wal = path.with_name(path.name + "-wal")
    result = {
        "database_mb": round(path.stat().st_size / 1024 / 1024, 2),
        "wal_mb": round(wal.stat().st_size / 1024 / 1024, 2) if wal.exists() else 0.0,
        "reclaimable_mb": round(freelist * page_size / 1024 / 1024, 2),
        "page_count": page_count,
        "rows": counts,
    }
    _STORAGE_CACHE = (now, result)
    return result


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
