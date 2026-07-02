from __future__ import annotations

import json
import os
import re
import threading
import time
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


def _data_dir() -> Path:
    config_path = Path(os.getenv("APP_CONFIG_PATH", "./data/config.json"))
    config_path.parent.mkdir(parents=True, exist_ok=True)
    return config_path.parent


def _rate_path() -> Path:
    return _data_dir() / "binance_rate_state.json"


def _cache_path() -> Path:
    return _data_dir() / "binance_cache.json"


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as file:
                return json.load(file)
    except Exception:
        return default
    return default


def _write_json(path: Path, value: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
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


def before_request(weight: int, budget_per_minute: int = 600) -> None:
    now = time.time()
    with _LOCK:
        state = _read_json(_rate_path(), {})
        cooldown_until = float(state.get("cooldown_until") or 0)
        if cooldown_until > now:
            raise BinanceRateLimitError(
                f"Binance REST 正在限流等待，预计 {int(cooldown_until - now)} 秒后恢复。",
                retry_after=cooldown_until - now,
                status_code=429,
            )
        bucket = _minute_bucket(now)
        if int(state.get("bucket") or -1) != bucket:
            state = {
                **state,
                "bucket": bucket,
                "used_estimated": 0,
                "last_error": "",
            }
        used = int(state.get("used_estimated") or 0)
        if used + weight > budget_per_minute:
            wait = (bucket + 1) * 60 - now + 1
            state["cooldown_until"] = now + wait
            state["last_error"] = f"本地 REST 预算达到 {budget_per_minute}/min，等待下一分钟恢复。"
            _write_json(_rate_path(), state)
            raise BinanceRateLimitError(state["last_error"], retry_after=wait, status_code=429)
        state["used_estimated"] = used + weight
        state["updated_at"] = now
        _write_json(_rate_path(), state)


def after_response(headers: Any) -> None:
    with _LOCK:
        state = _read_json(_rate_path(), {})
        used_header = headers.get("X-MBX-USED-WEIGHT-1M") if headers else None
        order_10s = headers.get("X-MBX-ORDER-COUNT-10S") if headers else None
        order_1m = headers.get("X-MBX-ORDER-COUNT-1M") if headers else None
        if used_header is not None:
            try:
                state["used_weight_1m"] = int(used_header)
            except ValueError:
                state["used_weight_1m"] = used_header
        if order_10s is not None:
            state["order_count_10s"] = order_10s
        if order_1m is not None:
            state["order_count_1m"] = order_1m
        state["updated_at"] = time.time()
        _write_json(_rate_path(), state)


def register_rate_error(status_code: int, text: str, retry_after_header: str | None = None) -> BinanceRateLimitError:
    retry_after = retry_after_from_text(text, retry_after_header)
    if retry_after is None:
        retry_after = 600 if status_code == 429 else 3600
    if status_code == 418:
        retry_after += 60
    now = time.time()
    with _LOCK:
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
    state = _read_json(_rate_path(), {})
    now = time.time()
    cooldown_until = float(state.get("cooldown_until") or 0)
    return {
        **state,
        "cooldown_active": cooldown_until > now,
        "cooldown_remaining_seconds": max(0, int(cooldown_until - now)),
    }


def cache_get(key: str, ttl_seconds: int) -> CacheEntry | None:
    now = time.time()
    with _LOCK:
        cache = _read_json(_cache_path(), {})
        item = cache.get(key)
        if not item:
            return None
        ts = float(item.get("ts") or 0)
        age = now - ts
        if age <= ttl_seconds:
            return CacheEntry(item.get("value"), age, False)
    return None


def cache_set(key: str, value: Any) -> None:
    now = time.time()
    with _LOCK:
        cache = _read_json(_cache_path(), {})
        cache[key] = {"ts": now, "value": value}
        # Avoid unbounded growth from per-symbol kline keys.
        if len(cache) > 500:
            ordered = sorted(cache.items(), key=lambda row: float(row[1].get("ts") or 0), reverse=True)
            cache = dict(ordered[:400])
        _write_json(_cache_path(), cache)


def cache_status() -> dict[str, Any]:
    cache = _read_json(_cache_path(), {})
    now = time.time()
    return {
        "entries": len(cache),
        "newest_age_seconds": min((now - float(item.get("ts") or 0) for item in cache.values()), default=None),
        "oldest_age_seconds": max((now - float(item.get("ts") or 0) for item in cache.values()), default=None),
    }
