from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_STATE: dict[str, Any] = {
    "bot_status": "paused",
    "stage": "growth",
    "equity_high_watermark": 0.0,
    "equity_guard_mode": "",
    "extreme_sprint_start_equity": 0.0,
    "extreme_sprint_equity_high_watermark": 0.0,
    "strategy_release_equity_id": None,
    "strategy_release_start_equity": 0.0,
    "strategy_release_equity_high_watermark": 0.0,
    "daily_start_equity": 0.0,
    "daily_session_date": None,
    "daily_session_started_at": None,
    "daily_realized_pnl": 0.0,
    "consecutive_losses": 0,
    "cooldown_until": None,
    "last_error": "",
    "risk_warning_active": False,
    "hard_stop_triggered": False,
    "hard_stop_reason": "",
    "updated_at": None,
}


_STATE_LOCK = threading.RLock()


def state_path() -> Path:
    config_path = Path(os.getenv("APP_CONFIG_PATH", "./data/config.json"))
    return config_path.with_name("state.json")


def load_state() -> dict[str, Any]:
    state = DEFAULT_STATE.copy()
    path = state_path()
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as file:
                state.update(json.load(file))
        except (OSError, json.JSONDecodeError):
            pass
    return state


def save_state(payload: dict[str, Any]) -> dict[str, Any]:
    with _STATE_LOCK:
        state = load_state()
        state.update(payload)
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        with tmp.open("w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, indent=2)
        tmp.replace(path)
        return state


def daily_session_state_updates(
    state: dict[str, Any],
    equity: float | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return an idempotent UTC trading-day reset without touching lifetime safeguards."""
    if equity is None:
        return {}
    now = now or datetime.now(timezone.utc)
    session_date = now.astimezone(timezone.utc).date().isoformat()
    if state.get("daily_session_date") == session_date and float(state.get("daily_start_equity") or 0) > 0:
        return {}
    return {
        "daily_session_date": session_date,
        "daily_session_started_at": now.isoformat(),
        "daily_start_equity": float(equity),
        "daily_realized_pnl": 0.0,
    }
