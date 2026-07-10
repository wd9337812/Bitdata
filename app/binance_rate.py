from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class BinanceRateLimitError(RuntimeError):
    def __init__(self, message: str, retry_after: float | None = None, status_code: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.status_code = status_code


@dataclass
class CacheEntry:
    value: Any
    age_seconds: float
    stale: bool


_LOCK = threading.Lock()
_REQUEST_PRIORITY: ContextVar[str] = ContextVar("binance_request_priority", default="normal")


@contextmanager
def request_priority(priority: str):
    token = _REQUEST_PRIORITY.set(priority)
    try:
        yield
    finally:
        _REQUEST_PRIORITY.reset(token)


def _data_dir() -> Path:
    config_path = Path(os.getenv("APP_CONFIG_PATH", "./data/config.json"))
    config_path.parent.mkdir(parents=True, exist_ok=True)
    return config_path.parent


def _rate_path() -> Path:
    return _data_dir() / "binance_rate_state.json"


@contextmanager
def _process_rate_lock():
    path = _data_dir() / "binance_rate_state.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as file:
        if os.name == "nt":
            import msvcrt

            file.seek(0, os.SEEK_END)
            if file.tell() == 0:
                file.write(b"0")
                file.flush()
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def _cache_path() -> Path:
    return _data_dir() / "binance_cache.db"


def _cache_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_cache_path(), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cache_entries (key TEXT PRIMARY KEY, ts REAL NOT NULL, value TEXT NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_entries_ts ON cache_entries(ts)")
    conn.commit()
    return conn


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as file:
                return json.load(file)
    except Exception:
        return default
    return default


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False)
    tmp.replace(path)


def _minute_bucket(now: float | None = None) -> int:
    return int((now or time.time()) // 60)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
        if seconds > 10_000_000_000:
            return max(0.0, seconds / 1000 - time.time())
        return max(0.0, seconds)
    except ValueError:
        return None


def retry_after_from_text(text: str, header_value: str | None = None) -> float | None:
    retry_after = _parse_retry_after(header_value)
    if retry_after is not None:
        return retry_after
    match = re.search(r"banned until (\d+)", text)
    if match:
        return max(0.0, int(match.group(1)) / 1000 - time.time())
    return None


def estimate_weight(path: str, params: dict[str, Any] | None = None, signed: bool = False) -> int:
    params = params or {}
    if path.endswith("/ticker/24hr"):
        return 1 if params.get("symbol") else 40
    if path.endswith("/premiumIndex"):
        return 1 if params.get("symbol") else 10
    if path.endswith("/klines") or path.endswith("Klines"):
        limit = int(params.get("limit") or 500)
        if limit < 100:
            return 1
        if limit < 500:
            return 2
        if limit <= 1000:
            return 5
        return 10
    if path.endswith("/depth"):
        limit = int(params.get("limit") or 500)
        if limit <= 50:
            return 2
        if limit <= 100:
            return 5
        if limit <= 500:
            return 10
        return 20
    if path.endswith("/account") or path.endswith("/balance") or path.endswith("/positionRisk"):
        return 5
    if path.endswith("/exchangeInfo") or path.endswith("/time"):
        return 1
    if signed:
        return 1
    return 1


def _rate_budgets(state: dict[str, Any], budget_per_minute: int | None = None) -> dict[str, int]:
    advertised = max(1, int(state.get("request_weight_limit") or 2400))
    if budget_per_minute is not None:
        normal = max(1, int(budget_per_minute))
        background = max(1, int(normal * 0.55))
        hard = max(normal, int(advertised * 0.50))
    else:
        background = max(1, int(advertised * 0.40))
        normal = max(background, int(advertised * 0.55))
        hard = max(normal, int(advertised * 0.75))
    critical = max(hard, int(advertised * 0.90))
    return {
        "advertised": advertised,
        "background": background,
        "normal": normal,
        "realtime": hard,
        "critical": critical,
    }


def before_request(weight: int, budget_per_minute: int | None = None, order_count: int = 0) -> None:
    now = time.time()
    with _LOCK, _process_rate_lock():
        state = _read_json(_rate_path(), {})
        cooldown_until = float(state.get("cooldown_until") or 0)
        priority = _REQUEST_PRIORITY.get()
        exchange_cooldown = int(state.get("last_status_code") or 0) in {418, 429}
        if cooldown_until > now and (priority != "critical" or exchange_cooldown):
            raise BinanceRateLimitError(
                f"Binance REST 正在限流等待，预计 {int(cooldown_until - now)} 秒后恢复。",
                retry_after=cooldown_until - now,
                status_code=429,
            )
        bucket = _minute_bucket(now)
        if int(state.get("bucket") or -1) != bucket:
            state = {**state, "bucket": bucket, "used_estimated": 0, "last_error": ""}
        header_used = int(state.get("used_weight_1m") or 0) if int(state.get("header_bucket") or -1) == bucket else 0
        used = max(int(state.get("used_estimated") or 0), header_used)
        budgets = _rate_budgets(state, budget_per_minute)
        priority_budget = budgets.get(priority, budgets["normal"])
        if used + weight > priority_budget:
            wait = max(1.0, (bucket + 1) * 60 - now + 0.25)
            if priority in {"background", "normal"}:
                raise BinanceRateLimitError(
                    f"REST 预算为实时交易预留，{priority}任务延后。",
                    retry_after=min(wait, 5.0),
                    status_code=429,
                )
            if priority == "realtime":
                raise BinanceRateLimitError(
                    "实时 REST 预算已达内部上限，等待下一分钟恢复。",
                    retry_after=wait,
                    status_code=429,
                )
            state["cooldown_until"] = now + wait
            state["last_error"] = "关键 REST 预算已达安全上限，等待下一分钟恢复。"
            _write_json(_rate_path(), state)
            raise BinanceRateLimitError(state["last_error"], retry_after=wait, status_code=429)

        order_10s_bucket = int(now // 10)
        if int(state.get("order_10s_bucket") or -1) != order_10s_bucket:
            state["order_10s_bucket"] = order_10s_bucket
            state["order_used_10s_estimated"] = 0
        if int(state.get("order_1m_bucket") or -1) != bucket:
            state["order_1m_bucket"] = bucket
            state["order_used_1m_estimated"] = 0
        if order_count:
            order_limit_10s = max(1, int(state.get("order_limit_10s") or 300))
            order_limit_1m = max(1, int(state.get("order_limit_1m") or 1200))
            order_used_10s = max(
                int(state.get("order_used_10s_estimated") or 0),
                int(state.get("order_count_10s") or 0) if int(state.get("order_header_10s_bucket") or -1) == order_10s_bucket else 0,
            )
            order_used_1m = max(
                int(state.get("order_used_1m_estimated") or 0),
                int(state.get("order_count_1m") or 0) if int(state.get("order_header_1m_bucket") or -1) == bucket else 0,
            )
            if order_used_10s + order_count > int(order_limit_10s * 0.9) or order_used_1m + order_count > int(order_limit_1m * 0.9):
                wait = max(1.0, (order_10s_bucket + 1) * 10 - now + 0.25)
                raise BinanceRateLimitError("订单频率达到内部安全上限。", retry_after=wait, status_code=429)
            state["order_used_10s_estimated"] = order_used_10s + order_count
            state["order_used_1m_estimated"] = order_used_1m + order_count

        state["used_estimated"] = used + weight
        state["budgets"] = budgets
        state["updated_at"] = now
        _write_json(_rate_path(), state)


def after_response(headers: Any) -> None:
    with _LOCK, _process_rate_lock():
        state = _read_json(_rate_path(), {})
        now = time.time()
        used_header = headers.get("X-MBX-USED-WEIGHT-1M") if headers else None
        order_10s = headers.get("X-MBX-ORDER-COUNT-10S") if headers else None
        order_1m = headers.get("X-MBX-ORDER-COUNT-1M") if headers else None
        if used_header is not None:
            try:
                state["used_weight_1m"] = int(used_header)
                state["header_bucket"] = _minute_bucket(now)
            except ValueError:
                state["used_weight_1m"] = used_header
        if order_10s is not None:
            state["order_count_10s"] = order_10s
            state["order_header_10s_bucket"] = int(now // 10)
        if order_1m is not None:
            state["order_count_1m"] = order_1m
            state["order_header_1m_bucket"] = _minute_bucket(now)
        state.pop("last_status_code", None)
        state["updated_at"] = now
        _write_json(_rate_path(), state)


def configure_exchange_limits(exchange_info: dict[str, Any]) -> None:
    updates: dict[str, int] = {}
    for item in exchange_info.get("rateLimits") or []:
        limit_type = str(item.get("rateLimitType") or "")
        interval = str(item.get("interval") or "")
        interval_num = int(item.get("intervalNum") or 1)
        limit = int(item.get("limit") or 0)
        if limit_type == "REQUEST_WEIGHT" and interval == "MINUTE" and interval_num == 1:
            updates["request_weight_limit"] = limit
        elif limit_type == "ORDERS" and interval == "SECOND" and interval_num == 10:
            updates["order_limit_10s"] = limit
        elif limit_type == "ORDERS" and interval == "MINUTE" and interval_num == 1:
            updates["order_limit_1m"] = limit
    if not updates:
        return
    with _LOCK, _process_rate_lock():
        state = _read_json(_rate_path(), {})
        state.update(updates)
        state["limits_updated_at"] = time.time()
        _write_json(_rate_path(), state)


def register_rate_error(status_code: int, text: str, retry_after_header: str | None = None) -> BinanceRateLimitError:
    retry_after = retry_after_from_text(text, retry_after_header)
    if retry_after is None:
        retry_after = 600 if status_code == 429 else 3600
    if status_code == 418:
        retry_after += 60
    now = time.time()
    with _LOCK, _process_rate_lock():
        state = _read_json(_rate_path(), {})
        state.update(
            {
                "cooldown_until": now + retry_after,
                "last_error": text,
                "last_status_code": status_code,
                "updated_at": now,
            }
        )
        _write_json(_rate_path(), state)
    return BinanceRateLimitError(text, retry_after=retry_after, status_code=status_code)


def rate_status() -> dict[str, Any]:
    with _LOCK, _process_rate_lock():
        state = _read_json(_rate_path(), {})
    now = time.time()
    cooldown_until = float(state.get("cooldown_until") or 0)
    budgets = _rate_budgets(state)
    bucket = _minute_bucket(now)
    used = max(
        int(state.get("used_estimated") or 0) if int(state.get("bucket") or -1) == bucket else 0,
        int(state.get("used_weight_1m") or 0) if int(state.get("header_bucket") or -1) == bucket else 0,
    )
    return {
        **state,
        "budgets": budgets,
        "used_current_minute": used,
        "normal_budget_used_pct": round(used / max(1, budgets["normal"]) * 100, 2),
        "exchange_limit_used_pct": round(used / max(1, budgets["advertised"]) * 100, 2),
        "cooldown_active": cooldown_until > now,
        "cooldown_remaining_seconds": max(0, int(cooldown_until - now)),
    }


def cache_get(key: str, ttl_seconds: int) -> CacheEntry | None:
    now = time.time()
    try:
        with closing(_cache_connect()) as conn:
            row = conn.execute("SELECT ts, value FROM cache_entries WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        age = now - float(row[0])
        if age <= ttl_seconds:
            return CacheEntry(json.loads(row[1]), age, False)
    except (sqlite3.Error, json.JSONDecodeError):
        return None
    return None


def cache_set(key: str, value: Any) -> None:
    now = time.time()
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    try:
        with closing(_cache_connect()) as conn:
            conn.execute(
                "INSERT INTO cache_entries(key, ts, value) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET ts=excluded.ts, value=excluded.value",
                (key, now, encoded),
            )
            count = int(conn.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0])
            if count > 600:
                conn.execute(
                    "DELETE FROM cache_entries WHERE key IN (SELECT key FROM cache_entries ORDER BY ts ASC LIMIT ?)",
                    (count - 450,),
                )
            conn.commit()
    except (sqlite3.Error, TypeError, ValueError):
        return


def cache_status() -> dict[str, Any]:
    now = time.time()
    try:
        with closing(_cache_connect()) as conn:
            row = conn.execute("SELECT COUNT(*), MAX(ts), MIN(ts) FROM cache_entries").fetchone()
        return {
            "backend": "sqlite_wal",
            "entries": int(row[0] or 0),
            "newest_age_seconds": now - float(row[1]) if row[1] else None,
            "oldest_age_seconds": now - float(row[2]) if row[2] else None,
            "size_bytes": _cache_path().stat().st_size if _cache_path().exists() else 0,
        }
    except sqlite3.Error as exc:
        return {"backend": "sqlite_wal", "entries": 0, "last_error": str(exc)}
