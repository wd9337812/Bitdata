from __future__ import annotations

import json
import os
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
    "updated_at": None,
}


def state_path() -> Path:
    config_path = Path(os.getenv("APP_CONFIG_PATH", "./data/config.json"))
    return config_path.with_name("state.json")


def load_state() -> dict[str, Any]:
    state = DEFAULT_STATE.copy()
    path = state_path()
    if path.exists():
        with path.open("r", encoding="utf-8") as file:
            state.update(json.load(file))
    return state


def save_state(payload: dict[str, Any]) -> dict[str, Any]:
    state = load_state()
    state.update(payload)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
    return state
