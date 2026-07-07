from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_LOCK = threading.Lock()


def snapshot_path() -> Path:
    config_path = Path(os.getenv("APP_CONFIG_PATH", "./data/config.json"))
    return config_path.with_name("runtime_snapshot.json")


def read_runtime_snapshot() -> dict[str, Any]:
    path = snapshot_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    updated_epoch = float(data.get("updated_epoch") or 0)
    return {**data, "age_seconds": max(0.0, time.time() - updated_epoch) if updated_epoch else None}


def update_runtime_snapshot(**updates: Any) -> dict[str, Any]:
    with _LOCK:
        current = read_runtime_snapshot()
        current.pop("age_seconds", None)
        current.update(updates)
        current["updated_at"] = datetime.now(timezone.utc).isoformat()
        current["updated_epoch"] = time.time()
        path = snapshot_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(current, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
        return current


def market_rows_from_scan(scan: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in scan.get("candidates") or []:
        symbol = str(candidate.get("symbol") or "")
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        ticker = candidate.get("ticker") or {}
        rows.append(
            {
                "symbol": symbol,
                "last": ticker.get("last"),
                "change_pct": ticker.get("change_pct"),
                "volume_usdt_b": ticker.get("volume_usdt_b"),
                "funding_pct": (candidate.get("derivatives") or {}).get("funding_rate_pct"),
            }
        )
    return rows
