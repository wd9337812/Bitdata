from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any
from urllib.parse import urlencode

import requests


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
        response = requests.get(self.base_url + path, params=params or {}, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def signed_request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self.api_key or not self.api_secret:
            raise ValueError("Binance API key and secret are required for signed requests.")
        payload = dict(params or {})
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
        response.raise_for_status()
        return response.json()

    def exchange_info(self) -> Any:
        return self.public_get("/fapi/v1/exchangeInfo")

    def klines(self, symbol: str, interval: str = "4h", limit: int = 1000) -> list[list[Any]]:
        return self.public_get("/fapi/v1/klines", {"symbol": symbol.upper(), "interval": interval, "limit": limit})

    def ticker_24h(self, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        data = self.public_get("/fapi/v1/ticker/24hr")
        if symbols:
            allowed = {symbol.upper() for symbol in symbols}
            data = [item for item in data if item["symbol"] in allowed]
        return data

    def premium_index(self, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        data = self.public_get("/fapi/v1/premiumIndex")
        if symbols:
            allowed = {symbol.upper() for symbol in symbols}
            data = [item for item in data if item["symbol"] in allowed]
        return data

    def account(self) -> Any:
        return self.signed_request("GET", "/fapi/v2/account")

    def position_risk(self) -> Any:
        return self.signed_request("GET", "/fapi/v2/positionRisk")

    def open_orders(self, symbol: str | None = None) -> Any:
        params = {"symbol": symbol.upper()} if symbol else {}
        return self.signed_request("GET", "/fapi/v1/openOrders", params)

    def cancel_all_open_orders(self, symbol: str) -> Any:
        return self.signed_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol.upper()})

    def set_leverage(self, symbol: str, leverage: int) -> Any:
        return self.signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol.upper(), "leverage": leverage})

    def place_market_order(self, symbol: str, side: str, quantity: float, reduce_only: bool = False) -> Any:
        params = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": "MARKET",
            "quantity": quantity,
            "reduceOnly": "true" if reduce_only else "false",
        }
        return self.signed_request("POST", "/fapi/v1/order", params)

    def place_stop_market(self, symbol: str, side: str, stop_price: float, close_position: bool = True) -> Any:
        params = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": "STOP_MARKET",
            "stopPrice": stop_price,
            "workingType": "MARK_PRICE",
            "closePosition": "true" if close_position else "false",
        }
        return self.signed_request("POST", "/fapi/v1/order", params)

    def place_take_profit_market(self, symbol: str, side: str, stop_price: float, close_position: bool = True) -> Any:
        params = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": stop_price,
            "workingType": "MARK_PRICE",
            "closePosition": "true" if close_position else "false",
        }
        return self.signed_request("POST", "/fapi/v1/order", params)

    def place_limit_order(self, symbol: str, side: str, quantity: float, price: float, reduce_only: bool = False) -> Any:
        params = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": quantity,
            "price": price,
            "reduceOnly": "true" if reduce_only else "false",
        }
        return self.signed_request("POST", "/fapi/v1/order", params)
