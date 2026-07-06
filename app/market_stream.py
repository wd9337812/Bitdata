from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests
import websockets

from app.binance_rate import after_response, before_request, estimate_weight
from app.opportunity_queue import enqueue_opportunity


_THREAD: threading.Thread | None = None
_STOP = threading.Event()
_LOCK = threading.Lock()


def data_dir() -> Path:
    config_path = Path(os.getenv("APP_CONFIG_PATH", "./data/config.json"))
    config_path.parent.mkdir(parents=True, exist_ok=True)
    return config_path.parent


def snapshot_path() -> Path:
    return data_dir() / "market_stream.json"


def intent_path() -> Path:
    return data_dir() / "stream_symbols.json"


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
        "triggers": [],
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
    intent = read_stream_intent()
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
        "intent_symbols": intent.get("symbols", []),
        "intent_count": len(intent.get("symbols", [])),
        "intent_updated_at": intent.get("updated_at"),
    }


def read_stream_intent() -> dict[str, Any]:
    try:
        path = intent_path()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return {
                "symbols": [str(symbol).upper() for symbol in data.get("symbols", [])],
                "sources": data.get("sources", {}),
                "updated_at": data.get("updated_at"),
            }
    except Exception:
        pass
    return {"symbols": [], "sources": {}, "updated_at": None}


def write_stream_intent(
    *,
    hot_symbols: list[str] | None = None,
    candidate_symbols: list[str] | None = None,
    position_symbols: list[str] | None = None,
    live_credit_symbols: list[str] | None = None,
) -> None:
    sources = {
        "hot": _dedupe_symbols(hot_symbols or []),
        "candidates": _dedupe_symbols(candidate_symbols or []),
        "positions": _dedupe_symbols(position_symbols or []),
        "live_credit": _dedupe_symbols(live_credit_symbols or []),
    }
    symbols = _dedupe_symbols(
        sources["positions"]
        + sources["candidates"]
        + sources["hot"]
        + sources["live_credit"]
    )
    payload = {"symbols": symbols, "sources": sources, "updated_at": _now_iso()}
    path = intent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


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


def stream_triggers(max_age_seconds: int = 180, limit: int = 40) -> list[dict[str, Any]]:
    state = read_snapshot()
    events = []
    for event in state.get("triggers", []) or []:
        if _fresh(event, max_age_seconds):
            events.append(event)
    events.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return events[:limit]


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


def _append_symbol(symbols: list[str], symbol: str) -> None:
    upper = str(symbol).upper().strip()
    if upper.endswith("USDT") and upper not in symbols:
        symbols.append(upper)


def _dedupe_symbols(symbols: list[str]) -> list[str]:
    result: list[str] = []
    for symbol in symbols:
        _append_symbol(result, symbol)
    return result


def _discover_stream_symbols(config: dict[str, Any], limit: int) -> list[str]:
    base_url = str(config.get("binance_base_url", "https://fapi.binance.com")).rstrip("/")
    min_volume = float(config.get("min_24h_volume_usdt", 30_000_000))
    try:
        before_request(estimate_weight("/fapi/v1/exchangeInfo"))
        exchange = requests.get(f"{base_url}/fapi/v1/exchangeInfo", timeout=10)
        after_response(exchange.headers)
        exchange.raise_for_status()
        before_request(estimate_weight("/fapi/v1/ticker/24hr"))
        tickers_response = requests.get(f"{base_url}/fapi/v1/ticker/24hr", timeout=10)
        after_response(tickers_response.headers)
        tickers_response.raise_for_status()
        coin_symbols = {
            item["symbol"]
            for item in exchange.json().get("symbols", [])
            if item.get("contractType") == "PERPETUAL"
            and item.get("underlyingType") == "COIN"
            and item.get("status") == "TRADING"
            and item.get("quoteAsset") == "USDT"
        }
        ranked = [
            (item["symbol"], float(item.get("quoteVolume", 0)))
            for item in tickers_response.json()
            if item.get("symbol") in coin_symbols and float(item.get("quoteVolume", 0)) >= min_volume
        ]
        ranked.sort(key=lambda row: row[1], reverse=True)
        return [symbol for symbol, _ in ranked[:limit]]
    except Exception:
        return []


def _symbols_from_config(config: dict[str, Any]) -> list[str]:
    symbols: list[str] = []
    limit = int(config.get("market_stream_max_symbols", config.get("max_scan_symbols", 30)))
    intent = read_stream_intent() if config.get("market_stream_dynamic_enabled", True) else {"symbols": [], "sources": {}}
    sources = intent.get("sources") or {}
    if config.get("stream_include_positions", True):
        for symbol in sources.get("positions", []):
            _append_symbol(symbols, symbol)
    if config.get("websocket_trigger_enabled", True):
        for event in stream_triggers(
            max_age_seconds=int(config.get("websocket_trigger_max_age_seconds", 180)),
            limit=int(config.get("websocket_trigger_scan_limit", 40)),
        ):
            _append_symbol(symbols, str(event.get("symbol") or ""))
    for key in ("stage1_symbols", "symbols", "stage2_symbols"):
        for symbol in config.get(key, []) or []:
            _append_symbol(symbols, symbol)
    hot_limit = int(config.get("stream_hot_symbols_limit", 25))
    for symbol in (sources.get("candidates", []) + sources.get("hot", []))[:hot_limit]:
        _append_symbol(symbols, symbol)
    if config.get("stream_include_live_credit", True):
        for symbol in sources.get("live_credit", []):
            _append_symbol(symbols, symbol)
    if config.get("market_stream_auto_discover", True):
        for symbol in _discover_stream_symbols(config, limit):
            _append_symbol(symbols, symbol)
            if len(symbols) >= limit:
                break
    return symbols[:limit]


def _symbol_change_pct(old: list[str], new: list[str]) -> float:
    old_set = set(old)
    new_set = set(new)
    if not old_set and not new_set:
        return 0.0
    changed = len(old_set.symmetric_difference(new_set))
    base = max(len(old_set), len(new_set), 1)
    return changed / base * 100


def _combined_url(path: str, streams: list[str]) -> str:
    return f"wss://fstream.binance.com/{path}/stream?streams=" + "/".join(streams)


def _public_stream_url(symbols: list[str]) -> str:
    streams: list[str] = []
    for symbol in symbols:
        streams.append(f"{symbol.lower()}@depth5@500ms")
    return _combined_url("public", streams)


def _market_stream_url(symbols: list[str], interval: str) -> str:
    streams: list[str] = []
    for symbol in symbols:
        lower = symbol.lower()
        streams.extend(
            [
                f"{lower}@ticker",
                f"{lower}@kline_{interval}",
            ]
        )
    return _combined_url("market", streams)


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


def _append_trigger_event(
    state: dict[str, Any],
    symbol: str,
    interval: str,
    row: list[Any],
    *,
    move_pct_threshold: float,
    quote_volume_threshold: float,
    max_events: int,
) -> None:
    try:
        open_price = float(row[1])
        close = float(row[4])
        quote_volume = float(row[7])
    except (TypeError, ValueError, IndexError):
        return
    signed_move_pct = (close - open_price) / close * 100 if close else 0.0
    move_pct = abs(signed_move_pct)
    if move_pct < move_pct_threshold and quote_volume < quote_volume_threshold:
        return
    event = {
        "symbol": symbol,
        "interval": interval,
        "type": "kline_trigger",
        "move_pct": round(move_pct, 4),
        "signed_move_pct": round(signed_move_pct, 4),
        "direction_hint": "LONG" if signed_move_pct > 0 else "SHORT" if signed_move_pct < 0 else "",
        "quote_volume": round(quote_volume, 4),
        "updated_at": _now_iso(),
        "source": "websocket",
    }
    existing = [
        item for item in state.get("triggers", []) or []
        if not (item.get("symbol") == symbol and item.get("interval") == interval)
    ]
    state["triggers"] = [event] + existing[: max(0, max_events - 1)]
    enqueue_opportunity(
        symbol=symbol,
        event_type="kline_trigger",
        direction_hint=event["direction_hint"],
        move_pct=signed_move_pct,
        quote_volume=quote_volume,
        interval=interval,
        source="websocket",
        features={"trigger_move_pct": move_pct_threshold, "trigger_quote_volume_usdt": quote_volume_threshold},
        max_events=max_events,
    )


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


async def _consume_stream(
    url: str,
    symbols: list[str],
    state: dict[str, Any],
    max_session_seconds: int,
    *,
    trigger_enabled: bool = True,
    trigger_move_pct: float = 0.35,
    trigger_quote_volume_usdt: float = 250_000,
    trigger_max_events: int = 80,
) -> None:
    started_at = time.time()
    async with websockets.connect(url, ping_interval=20, ping_timeout=10, close_timeout=5) as websocket:
        state.update({"connected": True, "symbols": symbols, "last_error": "", "updated_at": _now_iso()})
        write_snapshot(state)
        last_flush = 0.0
        while not _STOP.is_set():
            if time.time() - started_at >= max_session_seconds:
                return
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
                        if trigger_enabled:
                            _append_trigger_event(
                                state,
                                symbol,
                                kline_interval,
                                item["row"],
                                move_pct_threshold=trigger_move_pct,
                                quote_volume_threshold=trigger_quote_volume_usdt,
                                max_events=trigger_max_events,
                            )
                elif event_type == "depthUpdate" or payload.get("stream", "").endswith("depth5@500ms"):
                    symbol = str(data.get("s") or payload.get("stream", "").split("@")[0]).upper()
                    if symbol:
                        state.setdefault("depths", {})[symbol] = _depth_from_event(data)
                now = time.time()
                if now - last_flush >= 2:
                    state.update({"connected": True, "updated_at": _now_iso(), "symbols": symbols})
                    write_snapshot(state)
                    last_flush = now


async def _run_stream(config_provider: Callable[[], dict[str, Any]]) -> None:
    previous_symbols: list[str] = []
    while not _STOP.is_set():
        config = config_provider()
        if not config.get("market_stream_enabled", True):
            state = read_snapshot()
            state.update({"connected": False, "last_error": "market_stream_disabled", "updated_at": _now_iso()})
            write_snapshot(state)
            await asyncio.sleep(30)
            continue
        symbols = _symbols_from_config(config)
        rebuild_seconds = int(config.get("market_stream_rebuild_seconds", 60))
        threshold = float(config.get("market_stream_rotation_threshold_pct", 20.0))
        intent = read_stream_intent()
        required_positions = set((intent.get("sources") or {}).get("positions", []))
        missing_required = bool(required_positions - set(previous_symbols))
        if previous_symbols and not missing_required and _symbol_change_pct(previous_symbols, symbols) < threshold:
            symbols = previous_symbols
        previous_symbols = symbols
        interval = str(config.get("tournament_interval") or config.get("interval") or "5m")
        state = read_snapshot()
        state.update({"symbols": symbols, "connected": False, "updated_at": _now_iso()})
        write_snapshot(state)
        if not symbols:
            await asyncio.sleep(30)
            continue
        try:
            public_url = _public_stream_url(symbols)
            market_url = _market_stream_url(symbols, interval)
            trigger_kwargs = {
                "trigger_enabled": bool(config.get("websocket_trigger_enabled", True)),
                "trigger_move_pct": float(config.get("websocket_trigger_move_pct", 0.35)),
                "trigger_quote_volume_usdt": float(config.get("websocket_trigger_quote_volume_usdt", 250_000)),
                "trigger_max_events": int(config.get("websocket_trigger_max_events", 80)),
            }
            await asyncio.gather(
                _consume_stream(public_url, symbols, state, rebuild_seconds, **trigger_kwargs),
                _consume_stream(market_url, symbols, state, rebuild_seconds, **trigger_kwargs),
            )
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
