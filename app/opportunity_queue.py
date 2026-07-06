from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def data_dir() -> Path:
    config_path = Path(os.getenv("APP_CONFIG_PATH", "./data/config.json"))
    config_path.parent.mkdir(parents=True, exist_ok=True)
    return config_path.parent


def queue_path() -> Path:
    return data_dir() / "opportunity_events.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _write(events: list[dict[str, Any]]) -> None:
    path = queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"events": events, "updated_at": _now().isoformat()}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def read_raw_queue() -> dict[str, Any]:
    path = queue_path()
    if not path.exists():
        return {"events": [], "updated_at": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {"events": list(data.get("events") or []), "updated_at": data.get("updated_at")}
    except Exception as exc:
        return {"events": [], "updated_at": None, "last_error": str(exc)}


def score_event(event_type: str, move_pct: float, quote_volume: float) -> float:
    volume_score = min(max(quote_volume, 0.0) / 250_000 * 18.0, 45.0)
    move_score = min(abs(move_pct) * 22.0, 45.0)
    type_bonus = 12.0 if event_type in {"kline_trigger", "volume_breakout"} else 6.0
    return round(type_bonus + volume_score + move_score, 4)


def enqueue_opportunity(
    *,
    symbol: str,
    event_type: str,
    direction_hint: str | None = None,
    move_pct: float = 0.0,
    quote_volume: float = 0.0,
    interval: str = "",
    source: str = "websocket",
    features: dict[str, Any] | None = None,
    ttl_seconds: int = 240,
    max_events: int = 120,
    min_score: float = 20.0,
) -> dict[str, Any] | None:
    symbol = str(symbol or "").upper().strip()
    if not symbol.endswith("USDT"):
        return None
    score = score_event(event_type, move_pct, quote_volume)
    if score < min_score:
        return None
    direction = str(direction_hint or "").upper()
    if direction not in {"LONG", "SHORT"}:
        direction = "LONG" if move_pct > 0 else "SHORT" if move_pct < 0 else ""
    now = _now()
    event = {
        "symbol": symbol,
        "event_type": event_type,
        "direction_hint": direction,
        "score": score,
        "move_pct": round(float(move_pct), 4),
        "quote_volume": round(float(quote_volume), 4),
        "interval": interval,
        "source": source,
        "features": features or {},
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
    }
    existing = []
    for item in read_opportunities(max_age_seconds=ttl_seconds, limit=max_events):
        if not (item.get("symbol") == symbol and item.get("event_type") == event_type and item.get("interval") == interval):
            existing.append(item)
    events = [event] + existing
    events.sort(key=lambda item: (float(item.get("score") or 0), item.get("updated_at") or ""), reverse=True)
    _write(events[:max_events])
    return event


def read_opportunities(max_age_seconds: int = 240, limit: int = 50) -> list[dict[str, Any]]:
    now = _now()
    result = []
    for event in read_raw_queue().get("events", []):
        updated_at = _parse_time(event.get("updated_at"))
        expires_at = _parse_time(event.get("expires_at"))
        if expires_at and expires_at < now:
            continue
        if updated_at and (now - updated_at).total_seconds() > max_age_seconds:
            continue
        result.append(event)
    result.sort(key=lambda item: (float(item.get("score") or 0), item.get("updated_at") or ""), reverse=True)
    return result[:limit]


def opportunity_status(max_age_seconds: int = 240, limit: int = 20) -> dict[str, Any]:
    raw = read_raw_queue()
    events = read_opportunities(max_age_seconds=max_age_seconds, limit=limit)
    return {
        "enabled": True,
        "active_count": len(events),
        "stored_count": len(raw.get("events", [])),
        "updated_at": raw.get("updated_at"),
        "last_error": raw.get("last_error", ""),
        "events": events,
    }
