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
    "daily_start_equity": 0.0,
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
