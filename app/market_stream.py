from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests
import websockets

from app.binance_rate import after_response, before_request, estimate_weight
from app.order_book import ORDER_BOOKS, OrderBookGap
from app.opportunity_queue import enqueue_opportunity


_THREAD: threading.Thread | None = None
_STOP = threading.Event()
_LOCK = threading.RLock()
_MEMORY_STATE: dict[str, Any] | None = None
_MEMORY_PATH: Path | None = None
_TRADE_FLOW_STATE: dict[str, dict[str, Any]] = {}


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
    with _LOCK:
        if _MEMORY_STATE is not None and _MEMORY_PATH == path:
            return dict(_MEMORY_STATE)
    if not path.exists():
        return _empty_state()
    try:
        with path.open("r", encoding="utf-8") as file:
            return {**_empty_state(), **json.load(file)}
    except Exception as exc:
        state = _empty_state()
        state["last_error"] = str(exc)
        return state


def write_snapshot(state: dict[str, Any], *, persist: bool = True) -> None:
    global _MEMORY_PATH, _MEMORY_STATE
    path = snapshot_path()
    with _LOCK:
        _MEMORY_STATE = state
        _MEMORY_PATH = path
    if not persist:
        return
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
        "connection_count": int(state.get("connection_count") or 0),
        "stream_count": int(state.get("stream_count") or 0),
        "symbols_per_connection": int(state.get("symbols_per_connection") or 0),
        "ticker_count": len(state.get("tickers", {})),
        "depth_count": len(state.get("depths", {})),
        "full_orderbook_count": len(state.get("full_orderbook_symbols", []) or []),
        "full_orderbook_symbols": state.get("full_orderbook_symbols", []),
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
                "active_mode": data.get("active_mode"),
                "updated_at": data.get("updated_at"),
            }
    except Exception:
        pass
    return {"symbols": [], "sources": {}, "active_mode": None, "updated_at": None}


def write_stream_intent(
    *,
    hot_symbols: list[str] | None = None,
    candidate_symbols: list[str] | None = None,
    position_symbols: list[str] | None = None,
    live_credit_symbols: list[str] | None = None,
    shadow_symbols: list[str] | None = None,
    active_mode: str | None = None,
) -> None:
    sources = {
        "hot": _dedupe_symbols(hot_symbols or []),
        "candidates": _dedupe_symbols(candidate_symbols or []),
        "positions": _dedupe_symbols(position_symbols or []),
        "live_credit": _dedupe_symbols(live_credit_symbols or []),
        "shadow": _dedupe_symbols(shadow_symbols or []),
    }
    symbols = _dedupe_symbols(
        sources["positions"]
        + sources["shadow"]
        + sources["candidates"]
        + sources["hot"]
        + sources["live_credit"]
    )
    payload = {
        "symbols": symbols,
        "sources": sources,
        "active_mode": str(active_mode or ""),
        "updated_at": _now_iso(),
    }
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


def stream_tickers(max_age_seconds: int = 10) -> list[dict[str, Any]]:
    state = read_snapshot()
    return [item for item in (state.get("tickers") or {}).values() if _fresh(item, max_age_seconds)]


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
    min_volume = float(
        config.get(
            "market_stream_min_24h_volume_usdt",
            config.get("min_24h_volume_usdt", 30_000_000),
        )
    )
    try:
        streamed = stream_tickers(max_age_seconds=15)
        before_request(estimate_weight("/fapi/v1/exchangeInfo"))
        exchange = requests.get(f"{base_url}/fapi/v1/exchangeInfo", timeout=10)
        after_response(exchange.headers)
        exchange.raise_for_status()
        tickers = streamed
        if len(tickers) < 50:
            before_request(estimate_weight("/fapi/v1/ticker/24hr"))
            tickers_response = requests.get(f"{base_url}/fapi/v1/ticker/24hr", timeout=10)
            after_response(tickers_response.headers)
            tickers_response.raise_for_status()
            tickers = tickers_response.json()
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
            for item in tickers
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


def _diff_depth_stream_url(symbols: list[str]) -> str:
    streams: list[str] = []
    for symbol in symbols:
        lower = symbol.lower()
        streams.extend([f"{lower}@depth@100ms", f"{lower}@bookTicker"])
    return _combined_url("public", streams)


def _trade_stream_url(symbols: list[str]) -> str:
    return _combined_url("market", [f"{symbol.lower()}@aggTrade" for symbol in symbols])


def _market_stream_url(symbols: list[str], interval: str, *, include_all_ticker: bool = True) -> str:
    streams: list[str] = ["!ticker@arr"] if include_all_ticker else []
    for symbol in symbols:
        lower = symbol.lower()
        streams.append(f"{lower}@kline_{interval}")
        if interval != "1m":
            streams.append(f"{lower}@kline_1m")
    return _combined_url("market", streams)


def _symbol_shards(symbols: list[str], size: int) -> list[list[str]]:
    shard_size = max(1, int(size))
    return [symbols[index : index + shard_size] for index in range(0, len(symbols), shard_size)]


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
    bids = data.get("b") or data.get("bids") or []
    asks = data.get("a") or data.get("asks") or []
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


def _book_ticker_metrics(data: dict[str, Any]) -> dict[str, Any]:
    bid = float(data.get("b") or 0)
    ask = float(data.get("a") or 0)
    bid_qty = float(data.get("B") or 0)
    ask_qty = float(data.get("A") or 0)
    mid = (bid + ask) / 2 if bid and ask else 0.0
    total_qty = bid_qty + ask_qty
    micro_price = (ask * bid_qty + bid * ask_qty) / total_qty if total_qty and bid and ask else mid
    return {
        "best_bid": bid,
        "best_ask": ask,
        "best_bid_qty": bid_qty,
        "best_ask_qty": ask_qty,
        "mid_price": mid,
        "micro_price": micro_price,
        "microprice_edge_bps": (micro_price - mid) / mid * 10_000 if mid else 0.0,
        "spread_pct": (ask - bid) / mid * 100 if mid else 999.0,
        "book_ticker_updated_at": _now_iso(),
    }


def _trade_flow_metrics(data: dict[str, Any], window_seconds: float) -> dict[str, Any]:
    symbol = str(data.get("s") or "").upper()
    event_ms = int(data.get("T") or data.get("E") or time.time() * 1000)
    notional = float(data.get("p") or 0) * float(data.get("q") or 0)
    is_taker_sell = bool(data.get("m"))
    flow = _TRADE_FLOW_STATE.setdefault(
        symbol,
        {"samples": deque(), "buy": 0.0, "sell": 0.0, "buy_count": 0, "sell_count": 0},
    )
    side = "sell" if is_taker_sell else "buy"
    count_key = f"{side}_count"
    flow["samples"].append((event_ms, side, notional))
    flow[side] += notional
    flow[count_key] += 1
    cutoff = event_ms - max(1.0, window_seconds) * 1000
    while flow["samples"] and flow["samples"][0][0] < cutoff:
        _, old_side, old_notional = flow["samples"].popleft()
        flow[old_side] = max(0.0, float(flow[old_side]) - old_notional)
        old_count_key = f"{old_side}_count"
        flow[old_count_key] = max(0, int(flow[old_count_key]) - 1)
    buy = float(flow["buy"])
    sell = float(flow["sell"])
    total = buy + sell
    return {
        "trade_flow_buy_notional": buy,
        "trade_flow_sell_notional": sell,
        "trade_flow_notional": total,
        "trade_flow_imbalance": (buy - sell) / total if total else 0.0,
        "trade_flow_buy_count": int(flow["buy_count"]),
        "trade_flow_sell_count": int(flow["sell_count"]),
        "trade_flow_window_seconds": window_seconds,
        "trade_flow_updated_at": _now_iso(),
    }


def _full_orderbook_symbols(
    config: dict[str, Any],
    symbols: list[str],
    intent: dict[str, Any],
) -> list[str]:
    active_mode = str(intent.get("active_mode") or config.get("_active_growth_mode") or config.get("growth_mode") or "")
    if active_mode != "yolo_scalp" or not config.get("orderbook_full_stream_enabled", True):
        return []
    sources = intent.get("sources") or {}
    ranked = _dedupe_symbols(
        list(sources.get("positions") or [])
        + list(sources.get("candidates") or [])
        + list(sources.get("hot") or [])
    )
    allowed = set(symbols)
    limit = max(0, int(config.get("orderbook_full_symbols_limit", 20)))
    return [symbol for symbol in ranked if symbol in allowed][:limit]


def _seed_order_book(base_url: str, symbol: str, limit: int) -> None:
    params = {"symbol": symbol, "limit": limit}
    before_request(estimate_weight("/fapi/v1/depth", params))
    response = requests.get(f"{base_url}/fapi/v1/depth", params=params, timeout=10)
    after_response(response.headers)
    response.raise_for_status()
    ORDER_BOOKS.seed(symbol, response.json())


async def _seed_order_books(config: dict[str, Any], symbols: list[str]) -> None:
    if not symbols:
        return
    base_url = str(config.get("binance_base_url", "https://fapi.binance.com")).rstrip("/")
    limit = int(config.get("orderbook_snapshot_limit", 100))
    batch_size = max(1, min(4, int(config.get("orderbook_snapshot_concurrency", 4))))
    for index in range(0, len(symbols), batch_size):
        await asyncio.gather(
            *[
                asyncio.to_thread(_seed_order_book, base_url, symbol, limit)
                for symbol in symbols[index : index + batch_size]
            ]
        )


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
    persist_seconds: float = 5.0,
    orderbook_config: dict[str, Any] | None = None,
    orderbook_symbols: list[str] | None = None,
    seed_orderbooks: bool = False,
) -> None:
    started_at = time.time()
    async with websockets.connect(
        url,
        ping_interval=20,
        ping_timeout=10,
        close_timeout=5,
        max_queue=2048,
    ) as websocket:
        if seed_orderbooks and orderbook_symbols:
            await _seed_order_books(orderbook_config or {}, orderbook_symbols)
        state.update({"connected": True, "symbols": symbols, "last_error": "", "updated_at": _now_iso()})
        write_snapshot(state)
        last_flush = 0.0
        while not _STOP.is_set():
            if time.time() - started_at >= max_session_seconds:
                return
            raw = await asyncio.wait_for(websocket.recv(), timeout=35)
            payload = json.loads(raw)
            data = payload.get("data") or {}
            if isinstance(data, list):
                with _LOCK:
                    for item in data:
                        if item.get("e") == "24hrTicker":
                            symbol = str(item.get("s") or "").upper()
                            if symbol:
                                state.setdefault("tickers", {})[symbol] = _ticker_from_event(item)
                    state.update({"connected": True, "updated_at": _now_iso(), "symbols": symbols})
                    write_snapshot(state, persist=time.time() - last_flush >= persist_seconds)
                    if time.time() - last_flush >= persist_seconds:
                        last_flush = time.time()
                continue
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
                        if orderbook_symbols and symbol in orderbook_symbols:
                            book = ORDER_BOOKS.apply(symbol, data)
                            if book is not None:
                                previous = state.setdefault("depths", {}).get(symbol, {})
                                state["depths"][symbol] = {**previous, **_depth_from_event(book)}
                        else:
                            state.setdefault("depths", {})[symbol] = _depth_from_event(data)
                elif event_type == "bookTicker":
                    symbol = str(data.get("s") or payload.get("stream", "").split("@")[0]).upper()
                    if symbol:
                        previous = state.setdefault("depths", {}).get(symbol, {})
                        state["depths"][symbol] = {**previous, **_book_ticker_metrics(data), "updated_at": _now_iso()}
                elif event_type == "aggTrade":
                    symbol = str(data.get("s") or payload.get("stream", "").split("@")[0]).upper()
                    if symbol:
                        flow = _trade_flow_metrics(
                            data,
                            float((orderbook_config or {}).get("yolo_scalp_trade_flow_window_seconds", 5.0)),
                        )
                        previous = state.setdefault("depths", {}).get(symbol, {})
                        state["depths"][symbol] = {**previous, **flow, "updated_at": _now_iso()}
                        trigger_notional = float((orderbook_config or {}).get("yolo_scalp_trade_flow_trigger_notional_usdt", 100_000.0))
                        trigger_imbalance = float((orderbook_config or {}).get("yolo_scalp_trade_flow_trigger_imbalance", 0.15))
                        if flow["trade_flow_notional"] >= trigger_notional and abs(flow["trade_flow_imbalance"]) >= trigger_imbalance:
                            enqueue_opportunity(
                                symbol=symbol,
                                event_type="trade_flow",
                                direction_hint="LONG" if flow["trade_flow_imbalance"] > 0 else "SHORT",
                                quote_volume=flow["trade_flow_notional"],
                                source="websocket_trade_flow",
                                features=flow,
                                max_events=trigger_max_events,
                                min_interval_seconds=5.0,
                            )
                now = time.time()
                if now - last_flush >= persist_seconds:
                    state.update({"connected": True, "updated_at": _now_iso(), "symbols": symbols})
                    write_snapshot(state)
                    last_flush = now
                else:
                    state.update({"connected": True, "updated_at": _now_iso(), "symbols": symbols})
                    write_snapshot(state, persist=False)


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
        rebuild_seconds = int(config.get("market_stream_rebuild_seconds", 300))
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
            full_orderbook_symbols = _full_orderbook_symbols(config, symbols, intent)
            partial_symbols = [symbol for symbol in symbols if symbol not in set(full_orderbook_symbols)]
            symbols_per_connection = max(10, int(config.get("market_stream_symbols_per_connection", 75)))
            market_shards = _symbol_shards(symbols, symbols_per_connection)
            partial_shards = _symbol_shards(partial_symbols, symbols_per_connection)
            trigger_kwargs = {
                "trigger_enabled": bool(config.get("websocket_trigger_enabled", True)),
                "trigger_move_pct": float(config.get("websocket_trigger_move_pct", 0.35)),
                "trigger_quote_volume_usdt": float(config.get("websocket_trigger_quote_volume_usdt", 250_000)),
                "trigger_max_events": int(config.get("websocket_trigger_max_events", 80)),
                "persist_seconds": float(config.get("market_stream_persist_seconds", 5.0)),
            }
            tasks = []
            for index, shard in enumerate(market_shards):
                tasks.append(
                    _consume_stream(
                        _market_stream_url(shard, interval, include_all_ticker=index == 0),
                        symbols,
                        state,
                        rebuild_seconds,
                        **trigger_kwargs,
                    )
                )
            for shard in partial_shards:
                tasks.append(
                    _consume_stream(
                        _public_stream_url(shard),
                        symbols,
                        state,
                        rebuild_seconds,
                        **trigger_kwargs,
                    )
                )
            if full_orderbook_symbols:
                tasks.append(
                    _consume_stream(
                        _diff_depth_stream_url(full_orderbook_symbols),
                        symbols,
                        state,
                        rebuild_seconds,
                        orderbook_config=config,
                        orderbook_symbols=full_orderbook_symbols,
                        seed_orderbooks=True,
                        **trigger_kwargs,
                    )
                )
                tasks.append(
                    _consume_stream(
                        _trade_stream_url(full_orderbook_symbols),
                        symbols,
                        state,
                        rebuild_seconds,
                        orderbook_config=config,
                        orderbook_symbols=full_orderbook_symbols,
                        **trigger_kwargs,
                    )
                )
            market_stream_count = sum(
                (1 if index == 0 else 0) + len(shard) * (1 if interval == "1m" else 2)
                for index, shard in enumerate(market_shards)
            )
            state.update(
                {
                    "full_orderbook_symbols": full_orderbook_symbols,
                    "connection_count": len(tasks),
                    "stream_count": market_stream_count + len(partial_symbols) + len(full_orderbook_symbols) * 3,
                    "symbols_per_connection": symbols_per_connection,
                }
            )
            write_snapshot(state)
            await asyncio.gather(*tasks)
        except OrderBookGap as exc:
            state = read_snapshot()
            state.update({"connected": False, "last_error": str(exc), "updated_at": _now_iso(), "symbols": symbols})
            write_snapshot(state)
            await asyncio.sleep(1)
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
