from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import websockets

from app.binance_client import BinanceFuturesClient


_THREAD: threading.Thread | None = None
_STOP = threading.Event()
_LOCK = threading.Lock()
_MEMORY_STATE: dict[str, Any] | None = None
_MEMORY_PATH: Path | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _data_dir() -> Path:
    config_path = Path(os.getenv("APP_CONFIG_PATH", "./data/config.json"))
    config_path.parent.mkdir(parents=True, exist_ok=True)
    return config_path.parent


def _snapshot_path() -> Path:
    return _data_dir() / "user_stream.json"


def _empty_state() -> dict[str, Any]:
    return {
        "connected": False,
        "initialized": False,
        "updated_at": None,
        "last_event_at": None,
        "account_updated_at": None,
        "last_event_type": None,
        "last_error": "",
        "reconnects": 0,
        "last_update_ms": 0,
        "account_update_ms": 0,
        "event_revisions": {},
        "account": {},
        "orders": {},
        "algo_orders": {},
        "recent_events": [],
    }


def _read_disk() -> dict[str, Any]:
    path = _snapshot_path()
    try:
        return {**_empty_state(), **json.loads(path.read_text(encoding="utf-8"))} if path.exists() else _empty_state()
    except (OSError, json.JSONDecodeError):
        return _empty_state()


def read_user_stream_snapshot() -> dict[str, Any]:
    path = _snapshot_path()
    with _LOCK:
        if _MEMORY_STATE is not None and _MEMORY_PATH == path:
            return dict(_MEMORY_STATE)
    return _read_disk()


def _write_state(state: dict[str, Any]) -> None:
    global _MEMORY_PATH, _MEMORY_STATE
    safe_state = dict(state)
    safe_state.pop("listen_key", None)
    with _LOCK:
        _MEMORY_STATE = safe_state
        path = _snapshot_path()
        _MEMORY_PATH = path
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(safe_state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)


def _event_age(value: Any) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())
    except ValueError:
        return None


def user_stream_status(max_age_seconds: int = 90) -> dict[str, Any]:
    state = read_user_stream_snapshot()
    age = _event_age(state.get("updated_at"))
    account_age = _event_age(state.get("account_updated_at"))
    return {
        "connected": bool(state.get("connected")) and age is not None and age <= max_age_seconds,
        "raw_connected": bool(state.get("connected")),
        "initialized": bool(state.get("initialized")),
        "age_seconds": age,
        "account_age_seconds": account_age,
        "last_event_at": state.get("last_event_at"),
        "last_event_type": state.get("last_event_type"),
        "last_error": state.get("last_error", ""),
        "reconnects": int(state.get("reconnects") or 0),
        "orders": len(state.get("orders") or {}),
        "algo_orders": len(state.get("algo_orders") or {}),
        "last_update_ms": int(state.get("last_update_ms") or 0),
    }


def seed_user_account(account: dict[str, Any]) -> None:
    state = read_user_stream_snapshot()
    account_copy = dict(account)
    account_copy["positions"] = list(account.get("positions") or [])
    account_copy["assets"] = list(account.get("assets") or [])
    now = _now_iso()
    state.update(
        {
            "initialized": True,
            "account": account_copy,
            "account_updated_at": now,
            "updated_at": now,
        }
    )
    _write_state(state)


def account_from_user_stream(max_age_seconds: int = 90) -> dict[str, Any] | None:
    state = read_user_stream_snapshot()
    age = _event_age(state.get("account_updated_at"))
    account = state.get("account") or {}
    if not state.get("initialized") or age is None or age > max_age_seconds or not account:
        return None
    return dict(account)


def _merge_account_update(state: dict[str, Any], payload: dict[str, Any]) -> None:
    event = payload.get("a") or {}
    account = dict(state.get("account") or {})
    assets_by_name = {str(item.get("asset") or item.get("a") or ""): dict(item) for item in account.get("assets") or []}
    for item in event.get("B") or []:
        asset = str(item.get("a") or "")
        previous = assets_by_name.get(asset, {})
        assets_by_name[asset] = {
            **previous,
            "asset": asset,
            "walletBalance": str(item.get("wb", previous.get("walletBalance", 0))),
            "crossWalletBalance": str(item.get("cw", previous.get("crossWalletBalance", 0))),
        }
    positions_by_key = {
        f"{item.get('symbol')}:{item.get('positionSide', 'BOTH')}": dict(item)
        for item in account.get("positions") or []
    }
    for item in event.get("P") or []:
        symbol = str(item.get("s") or "")
        side = str(item.get("ps") or "BOTH")
        key = f"{symbol}:{side}"
        previous = positions_by_key.get(key, {})
        positions_by_key[key] = {
            **previous,
            "symbol": symbol,
            "positionSide": side,
            "positionAmt": str(item.get("pa", previous.get("positionAmt", 0))),
            "entryPrice": str(item.get("ep", previous.get("entryPrice", 0))),
            "breakEvenPrice": str(item.get("bep", previous.get("breakEvenPrice", 0))),
            "unrealizedProfit": str(item.get("up", previous.get("unrealizedProfit", 0))),
            "isolatedWallet": str(item.get("iw", previous.get("isolatedWallet", 0))),
        }
    assets = list(assets_by_name.values())
    positions = list(positions_by_key.values())
    usdt = assets_by_name.get("USDT", {})
    account["assets"] = assets
    account["positions"] = positions
    if usdt:
        account["totalWalletBalance"] = str(usdt.get("walletBalance", account.get("totalWalletBalance", 0)))
        account["availableBalanceSource"] = "rest_snapshot"
    account["totalUnrealizedProfit"] = str(sum(float(item.get("unrealizedProfit") or 0) for item in positions))
    state["account"] = account
    state["initialized"] = bool(account)
    state["account_updated_at"] = _now_iso()


def _merge_event(state: dict[str, Any], payload: dict[str, Any]) -> None:
    event_ms = int(payload.get("E") or payload.get("T") or 0)
    event_type = str(payload.get("e") or "")
    revisions = dict(state.get("event_revisions") or {})
    if event_ms and event_ms < int(revisions.get(event_type) or 0):
        return
    if event_type == "ACCOUNT_UPDATE":
        _merge_account_update(state, payload)
        state["account_update_ms"] = max(event_ms, int(state.get("account_update_ms") or 0))
    elif event_type == "ORDER_TRADE_UPDATE":
        order = payload.get("o") or {}
        key = f"{order.get('s')}:{order.get('i')}"
        status = str(order.get("X") or order.get("x") or "").upper()
        if status in {"FILLED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}:
            state.setdefault("orders", {}).pop(key, None)
        else:
            state.setdefault("orders", {})[key] = order
    elif event_type == "ALGO_UPDATE":
        order = payload.get("o") or {}
        key = f"{order.get('s')}:{order.get('aid') or order.get('caid')}"
        status = str(order.get("X") or order.get("x") or order.get("S") or "").upper()
        if status in {"FINISHED", "FILLED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}:
            state.setdefault("algo_orders", {}).pop(key, None)
        else:
            state.setdefault("algo_orders", {})[key] = order
    elif event_type == "listenKeyExpired":
        state["connected"] = False
    now = _now_iso()
    state["last_event_at"] = now
    state["last_event_type"] = event_type
    state["updated_at"] = now
    state["last_error"] = ""
    state["last_update_ms"] = max(event_ms, int(state.get("last_update_ms") or 0))
    revisions[event_type] = max(event_ms, int(revisions.get(event_type) or 0))
    state["event_revisions"] = revisions
    recent = [{"event": event_type, "event_time": payload.get("E"), "updated_at": now}]
    state["recent_events"] = (recent + list(state.get("recent_events") or []))[:100]


async def _consume(config_provider: Callable[[], dict[str, Any]]) -> None:
    config = config_provider()
    client = BinanceFuturesClient(
        api_key=config.get("api_key", ""),
        api_secret=config.get("api_secret", ""),
        base_url=config.get("binance_base_url", "https://fapi.binance.com"),
    )
    listen_key = await asyncio.to_thread(client.start_user_stream)
    url = f"wss://fstream.binance.com/private/ws/{listen_key}"
    keepalive_seconds = int(config.get("user_stream_keepalive_seconds", 1800))
    session_seconds = int(config.get("user_stream_max_session_seconds", 82_800))
    started = time.monotonic()
    last_keepalive = started
    state = read_user_stream_snapshot()
    async with websockets.connect(url, ping_interval=150, ping_timeout=600, close_timeout=5) as websocket:
        state.update({"connected": True, "updated_at": _now_iso(), "last_error": ""})
        _write_state(state)
        while not _STOP.is_set() and time.monotonic() - started < session_seconds:
            now = time.monotonic()
            if now - last_keepalive >= keepalive_seconds:
                await asyncio.to_thread(client.keepalive_user_stream, listen_key)
                last_keepalive = now
            try:
                raw = await asyncio.wait_for(websocket.recv(), timeout=30)
            except asyncio.TimeoutError:
                state["updated_at"] = _now_iso()
                _write_state(state)
                continue
            payload = json.loads(raw)
            _merge_event(state, payload)
            _write_state(state)
    try:
        await asyncio.to_thread(client.close_user_stream, listen_key)
    except Exception:
        pass


async def _run(config_provider: Callable[[], dict[str, Any]]) -> None:
    while not _STOP.is_set():
        config = config_provider()
        if not config.get("user_stream_enabled", True) or not config.get("api_key"):
            state = read_user_stream_snapshot()
            state.update({"connected": False, "updated_at": _now_iso(), "last_error": "user_stream_disabled"})
            _write_state(state)
            await asyncio.sleep(30)
            continue
        try:
            await _consume(config_provider)
        except Exception as exc:
            state = read_user_stream_snapshot()
            state.update(
                {
                    "connected": False,
                    "updated_at": _now_iso(),
                    "last_error": str(exc),
                    "reconnects": int(state.get("reconnects") or 0) + 1,
                }
            )
            _write_state(state)
            await asyncio.sleep(max(1, int(config.get("user_stream_reconnect_seconds", 5))))


def start_user_stream_thread(config_provider: Callable[[], dict[str, Any]]) -> None:
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        return

    def target() -> None:
        asyncio.run(_run(config_provider))

    _STOP.clear()
    _THREAD = threading.Thread(target=target, name="user-stream", daemon=True)
    _THREAD.start()
