from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any
from urllib.parse import urlencode

import requests

from app.binance_rate import (
    after_response,
    before_request,
    cache_get,
    cache_set,
    estimate_weight,
    register_rate_error,
)
from app.market_stream import overlay_stream_kline, stream_depth, stream_ticker

INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}


class BinanceFuturesClient:
    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        base_url: str = "https://fapi.binance.com",
        timeout: int = 15,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def public_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        weight = estimate_weight(path, params, signed=False)
        before_request(weight)
        response = requests.get(self.base_url + path, params=params or {}, timeout=self.timeout)
        after_response(response.headers)
        if response.status_code in {418, 429}:
            raise register_rate_error(response.status_code, response.text, response.headers.get("Retry-After"))
        if not response.ok:
            raise RuntimeError(f"Binance API {response.status_code}: {response.text}")
        return response.json()

    def signed_request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self.api_key or not self.api_secret:
            raise ValueError("Binance API key and secret are required for signed requests.")
        payload = dict(params or {})
        weight = estimate_weight(path, payload, signed=True)
        before_request(weight)
        payload.setdefault("recvWindow", 10_000)
        payload["timestamp"] = int(time.time() * 1000)
        query = urlencode(payload, doseq=True)
        signature = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        payload["signature"] = signature
        headers = {"X-MBX-APIKEY": self.api_key}
        response = requests.request(
            method.upper(),
            self.base_url + path,
            params=payload,
            headers=headers,
            timeout=self.timeout,
        )
        after_response(response.headers)
        if response.status_code in {418, 429}:
            raise register_rate_error(response.status_code, response.text, response.headers.get("Retry-After"))
        if not response.ok:
            raise RuntimeError(f"Binance signed API {response.status_code}: {response.text}")
        return response.json()

    def exchange_info(self) -> Any:
        key = "exchange_info"
        cached = cache_get(key, 3600)
        if cached:
            return cached.value
        data = self.public_get("/fapi/v1/exchangeInfo")
        cache_set(key, data)
        return data

    def server_time(self) -> Any:
        return self.public_get("/fapi/v1/time")

    def time_offset_ms(self) -> int:
        local_time = int(time.time() * 1000)
        server_time = int(self.server_time().get("serverTime", 0))
        return local_time - server_time

    def klines(self, symbol: str, interval: str = "4h", limit: int = 1000) -> list[list[Any]]:
        key = f"klines:{symbol.upper()}:{interval}:{limit}"
        cached = cache_get(key, 20)
        if cached:
            return cached.value
        data = self.public_get("/fapi/v1/klines", {"symbol": symbol.upper(), "interval": interval, "limit": limit})
        data = overlay_stream_kline(data, symbol, interval)
        cache_set(key, data)
        return data

    def klines_history(self, symbol: str, interval: str = "4h", days: int = 30, warmup: int = 200) -> list[list[Any]]:
        key = f"klines_history:{symbol.upper()}:{interval}:{days}:{warmup}"
        cached = cache_get(key, 60)
        if cached:
            return overlay_stream_kline(cached.value, symbol, interval)
        interval_ms = INTERVAL_MS.get(interval)
        if interval_ms is None:
            return self.klines(symbol, interval, 1000)
        target_bars = int(days * 86_400_000 / interval_ms) + warmup
        target_bars = max(100, min(target_bars, 6000))
        rows: list[list[Any]] = []
        end_time: int | None = None
        while len(rows) < target_bars:
            batch_limit = min(1500, target_bars - len(rows))
            params: dict[str, Any] = {"symbol": symbol.upper(), "interval": interval, "limit": batch_limit}
            if end_time is not None:
                params["endTime"] = end_time
            batch = self.public_get("/fapi/v1/klines", params)
            if not batch:
                break
            rows = batch + rows
            first_open = int(batch[0][0])
            next_end = first_open - 1
            if end_time == next_end:
                break
            end_time = next_end
            if len(batch) < batch_limit:
                break
        dedup = {int(row[0]): row for row in rows}
        data = [dedup[key] for key in sorted(dedup)]
        data = overlay_stream_kline(data, symbol, interval)
        cache_set(key, data)
        return data

    def ticker_24h(self, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        key = "ticker_24h:all"
        cached = cache_get(key, 30)
        if cached:
            data = cached.value
        else:
            data = self.public_get("/fapi/v1/ticker/24hr")
            cache_set(key, data)
        if symbols:
            allowed = {symbol.upper() for symbol in symbols}
            data = [item for item in data if item["symbol"] in allowed]
            streamed = {item["symbol"]: item for item in data}
            for symbol in allowed:
                item = stream_ticker(symbol)
                if item:
                    streamed[symbol] = {**streamed.get(symbol, {}), **item}
            data = list(streamed.values())
        return data

    def premium_index(self, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        key = "premium_index:all"
        cached = cache_get(key, 30)
        if cached:
            data = cached.value
        else:
            data = self.public_get("/fapi/v1/premiumIndex")
            cache_set(key, data)
        if symbols:
            allowed = {symbol.upper() for symbol in symbols}
            data = [item for item in data if item["symbol"] in allowed]
        return data

    def depth(self, symbol: str, limit: int = 5) -> Any:
        item = stream_depth(symbol)
        if item and item.get("bids") and item.get("asks"):
            return {"bids": item["bids"], "asks": item["asks"]}
        key = f"depth:{symbol.upper()}:{limit}"
        cached = cache_get(key, 10)
        if cached:
            return cached.value
        data = self.public_get("/fapi/v1/depth", {"symbol": symbol.upper(), "limit": limit})
        cache_set(key, data)
        return data

    def account(self) -> Any:
        key = "account:v2"
        cached = cache_get(key, 45)
        if cached:
            return cached.value
        data = self.signed_request("GET", "/fapi/v2/account")
        cache_set(key, data)
        return data

    def account_live(self) -> Any:
        data = self.signed_request("GET", "/fapi/v2/account")
        cache_set("account:v2", data)
        return data

    def position_risk(self) -> Any:
        return self.signed_request("GET", "/fapi/v2/positionRisk")

    def open_orders(self, symbol: str | None = None) -> Any:
        params = {"symbol": symbol.upper()} if symbol else {}
        return self.signed_request("GET", "/fapi/v1/openOrders", params)

    def user_trades(self, symbol: str, limit: int = 100) -> Any:
        symbol = symbol.upper()
        key = f"user_trades:{symbol}:{limit}"
        cached = cache_get(key, 120)
        if cached:
            return cached.value
        data = self.signed_request("GET", "/fapi/v1/userTrades", {"symbol": symbol, "limit": limit})
        cache_set(key, data)
        return data

    def open_algo_orders(self, symbol: str | None = None) -> Any:
        params = {"symbol": symbol.upper()} if symbol else {}
        return self.signed_request("GET", "/fapi/v1/openAlgoOrders", params)

    def position_side_dual(self) -> Any:
        return self.signed_request("GET", "/fapi/v1/positionSide/dual")

    def cancel_all_open_orders(self, symbol: str) -> Any:
        return self.signed_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol.upper()})

    def set_leverage(self, symbol: str, leverage: int) -> Any:
        return self.signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol.upper(), "leverage": leverage})

    def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        reduce_only: bool = False,
        position_side: str | None = None,
    ) -> Any:
        params = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": "MARKET",
            "quantity": quantity,
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        if position_side:
            params["positionSide"] = position_side.upper()
        return self.signed_request("POST", "/fapi/v1/order", params)

    def place_stop_market(
        self,
        symbol: str,
        side: str,
        stop_price: float,
        close_position: bool = True,
        position_side: str | None = None,
    ) -> Any:
        params = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": "STOP_MARKET",
            "stopPrice": stop_price,
            "workingType": "MARK_PRICE",
            "closePosition": "true" if close_position else "false",
        }
        if position_side:
            params["positionSide"] = position_side.upper()
        return self.signed_request("POST", "/fapi/v1/order", params)

    def place_take_profit_market(
        self,
        symbol: str,
        side: str,
        stop_price: float,
        close_position: bool = True,
        position_side: str | None = None,
    ) -> Any:
        params = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": stop_price,
            "workingType": "MARK_PRICE",
            "closePosition": "true" if close_position else "false",
        }
        if position_side:
            params["positionSide"] = position_side.upper()
        return self.signed_request("POST", "/fapi/v1/order", params)

    def place_algo_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        trigger_price: float,
        close_position: bool = True,
        quantity: float | None = None,
        position_side: str | None = None,
        working_type: str = "MARK_PRICE",
    ) -> Any:
        params = {
            "algoType": "CONDITIONAL",
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": order_type.upper(),
            "triggerPrice": trigger_price,
            "workingType": working_type,
        }
        if close_position:
            params["closePosition"] = "true"
        elif quantity is not None:
            params["quantity"] = quantity
        if position_side:
            params["positionSide"] = position_side.upper()
        return self.signed_request("POST", "/fapi/v1/algoOrder", params)

    def place_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        reduce_only: bool = False,
        position_side: str | None = None,
    ) -> Any:
        params = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": quantity,
            "price": price,
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        if position_side:
            params["positionSide"] = position_side.upper()
        return self.signed_request("POST", "/fapi/v1/order", params)
