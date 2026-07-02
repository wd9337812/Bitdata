from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

import websockets


_THREAD: threading.Thread | None = None
_STOP = threading.Event()
_LOCK = threading.Lock()


def data_dir() -> Path:
    config_path = Path(os.getenv("APP_CONFIG_PATH", "./data/config.json"))
    config_path.parent.mkdir(parents=True, exist_ok=True)
    return config_path.parent


def snapshot_path() -> Path:
    return data_dir() / "market_stream.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_state() -> dict[str, Any]:
    return {
        "connected": False,
        "updated_at": None,
        "symbols": [],
        "tickers": {},
        "klines": {},
        "depths": {},
        "last_error": "",
    }


def read_snapshot() -> dict[str, Any]:
    path = snapshot_path()
    if not path.exists():
        return _empty_state()
    try:
        with path.open("r", encoding="utf-8") as file:
            return {**_empty_state(), **json.load(file)}
    except Exception as exc:
        state = _empty_state()
        state["last_error"] = str(exc)
        return state


def write_snapshot(state: dict[str, Any]) -> None:
    path = snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False)
    tmp.replace(path)


def stream_status(max_age_seconds: int = 15) -> dict[str, Any]:
    state = read_snapshot()
    updated_at = state.get("updated_at")
    age = None
    if updated_at:
        try:
            age = time.time() - datetime.fromisoformat(updated_at).timestamp()
        except ValueError:
            age = None
    return {
        "connected": bool(state.get("connected")) and (age is None or age <= max_age_seconds),
        "raw_connected": bool(state.get("connected")),
        "age_seconds": age,
        "symbols": state.get("symbols", []),
        "ticker_count": len(state.get("tickers", {})),
        "depth_count": len(state.get("depths", {})),
        "kline_count": sum(len(value) for value in (state.get("klines", {}) or {}).values()),
        "last_error": state.get("last_error", ""),
        "updated_at": updated_at,
    }


def _fresh(item: dict[str, Any] | None, max_age_seconds: int) -> bool:
    if not item:
        return False
    updated_at = item.get("updated_at")
    if not updated_at:
        return False
    try:
        age = time.time() - datetime.fromisoformat(updated_at).timestamp()
    except ValueError:
        return False
    return age <= max_age_seconds


def stream_ticker(symbol: str, max_age_seconds: int = 10) -> dict[str, Any] | None:
    state = read_snapshot()
    item = (state.get("tickers") or {}).get(symbol.upper())
    return item if _fresh(item, max_age_seconds) else None


def stream_depth(symbol: str, max_age_seconds: int = 5) -> dict[str, Any] | None:
    state = read_snapshot()
    item = (state.get("depths") or {}).get(symbol.upper())
    return item if _fresh(item, max_age_seconds) else None


def stream_kline(symbol: str, interval: str, max_age_seconds: int = 20) -> list[Any] | None:
    state = read_snapshot()
    item = ((state.get("klines") or {}).get(symbol.upper()) or {}).get(interval)
    if not _fresh(item, max_age_seconds):
        return None
    return item.get("row")


def overlay_stream_kline(rows: list[list[Any]], symbol: str, interval: str) -> list[list[Any]]:
    row = stream_kline(symbol, interval)
    if not row:
        return rows
    if not rows:
        return [row]
    current_open = int(row[0])
    result = list(rows)
    last_open = int(result[-1][0])
    if current_open == last_open:
        result[-1] = row
    elif current_open > last_open:
        result.append(row)
    return result


def _symbols_from_config(config: dict[str, Any]) -> list[str]:
    symbols: list[str] = []
    for key in ("stage1_symbols", "symbols", "stage2_symbols"):
        for symbol in config.get(key, []) or []:
            upper = str(symbol).upper().strip()
            if upper.endswith("USDT") and upper not in symbols:
                symbols.append(upper)
    return symbols[: int(config.get("max_observation_symbols", 20))]


def _stream_url(symbols: list[str], interval: str) -> str:
    streams: list[str] = []
    for symbol in symbols:
        lower = symbol.lower()
        streams.extend(
            [
                f"{lower}@ticker",
                f"{lower}@kline_{interval}",
                f"{lower}@depth5@500ms",
            ]
        )
    return "wss://fstream.binance.com/stream?" + urlencode({"streams": "/".join(streams)})


def _ticker_from_event(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": data.get("s"),
        "lastPrice": str(data.get("c", 0)),
        "priceChangePercent": str(data.get("P", 0)),
        "quoteVolume": str(data.get("q", 0)),
        "updated_at": _now_iso(),
        "source": "websocket",
    }


def _kline_from_event(data: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    kline = data.get("k") or {}
    symbol = str(kline.get("s") or data.get("s") or "").upper()
    interval = str(kline.get("i") or "")
    row = [
        int(kline.get("t", 0)),
        str(kline.get("o", 0)),
        str(kline.get("h", 0)),
        str(kline.get("l", 0)),
        str(kline.get("c", 0)),
        str(kline.get("v", 0)),
        int(kline.get("T", 0)),
        str(kline.get("q", 0)),
        int(kline.get("n", 0)),
        str(kline.get("V", 0)),
        str(kline.get("Q", 0)),
        "0",
    ]
    return symbol, interval, {
        "row": row,
        "closed": bool(kline.get("x")),
        "updated_at": _now_iso(),
        "source": "websocket",
    }


def _depth_from_event(data: dict[str, Any]) -> dict[str, Any]:
    bids = data.get("b") or []
    asks = data.get("a") or []
    spread_pct = 999.0
    depth_notional = 0.0
    if bids and asks:
        bid = float(bids[0][0])
        ask = float(asks[0][0])
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid * 100 if mid else 999.0
        bid_notional = sum(float(price) * float(qty) for price, qty in bids[:5])
        ask_notional = sum(float(price) * float(qty) for price, qty in asks[:5])
        depth_notional = min(bid_notional, ask_notional)
    return {
        "available": bool(bids and asks),
        "bids": bids,
        "asks": asks,
        "spread_pct": spread_pct,
        "depth_notional": depth_notional,
        "updated_at": _now_iso(),
        "source": "websocket",
    }


async def _run_stream(config_provider: Callable[[], dict[str, Any]]) -> None:
    while not _STOP.is_set():
        config = config_provider()
        if not config.get("market_stream_enabled", True):
            state = read_snapshot()
            state.update({"connected": False, "last_error": "market_stream_disabled", "updated_at": _now_iso()})
            write_snapshot(state)
            await asyncio.sleep(30)
            continue
        symbols = _symbols_from_config(config)
        interval = str(config.get("tournament_interval") or config.get("interval") or "5m")
        state = read_snapshot()
        state.update({"symbols": symbols, "connected": False, "updated_at": _now_iso()})
        write_snapshot(state)
        if not symbols:
            await asyncio.sleep(30)
            continue
        url = _stream_url(symbols, interval)
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10, close_timeout=5) as websocket:
                state = read_snapshot()
                state.update({"connected": True, "symbols": symbols, "last_error": "", "updated_at": _now_iso()})
                write_snapshot(state)
                last_flush = 0.0
                while not _STOP.is_set():
                    raw = await asyncio.wait_for(websocket.recv(), timeout=35)
                    payload = json.loads(raw)
                    data = payload.get("data") or {}
                    event_type = data.get("e")
                    with _LOCK:
                        if event_type == "24hrTicker":
                            symbol = str(data.get("s") or "").upper()
                            if symbol:
                                state.setdefault("tickers", {})[symbol] = _ticker_from_event(data)
                        elif event_type == "kline":
                            symbol, kline_interval, item = _kline_from_event(data)
                            if symbol and kline_interval:
                                state.setdefault("klines", {}).setdefault(symbol, {})[kline_interval] = item
                        elif payload.get("stream", "").endswith("depth5@500ms"):
                            symbol = str(data.get("s") or payload.get("stream", "").split("@")[0]).upper()
                            if symbol:
                                state.setdefault("depths", {})[symbol] = _depth_from_event(data)
                        now = time.time()
                        if now - last_flush >= 2:
                            state.update({"connected": True, "updated_at": _now_iso(), "symbols": symbols})
                            write_snapshot(state)
                            last_flush = now
        except Exception as exc:
            state = read_snapshot()
            state.update({"connected": False, "last_error": str(exc), "updated_at": _now_iso(), "symbols": symbols})
            write_snapshot(state)
            await asyncio.sleep(5)


def start_market_stream_thread(config_provider: Callable[[], dict[str, Any]]) -> None:
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        return

    def target() -> None:
        asyncio.run(_run_stream(config_provider))

    _STOP.clear()
    _THREAD = threading.Thread(target=target, name="market-stream", daemon=True)
    _THREAD.start()
