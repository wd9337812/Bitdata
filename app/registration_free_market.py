from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


class RegistrationFreeMarketClient:
    """Anonymous public market-data client. No API key or registration needed."""

    venue: str = "base"

    def last_price(self, symbol: str) -> float:
        raise NotImplementedError

    def klines(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError


@dataclass(frozen=True)
class OkxPublicClient(RegistrationFreeMarketClient):
    base_url: str = "https://www.okx.com"
    timeout: int = 15

    @staticmethod
    def instrument(symbol: str) -> str:
        base = symbol.removesuffix("USDT")
        return f"{base}-USDT-SWAP"

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        response = requests.get(
            f"{self.base_url}{path}",
            params=params,
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != "0":
            raise RuntimeError(f"OKX error: {payload}")
        return payload

    def last_price(self, symbol: str) -> float:
        payload = self._get(
            "/api/v5/market/tickers",
            {"instType": "SWAP", "instId": self.instrument(symbol)},
        )
        return float(payload["data"][0]["last"])

    def klines(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        after = str(end_ms)
        while True:
            params = {
                "instId": self.instrument(symbol),
                "bar": interval,
                "after": after,
                "limit": "100",
            }
            data = self._get("/api/v5/market/history-candles", params)["data"]
            if not data:
                break
            rows.extend(data)
            earliest = int(data[-1][0])
            if earliest <= start_ms:
                break
            after = str(earliest)
        return [
            {
                "open_time": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            }
            for row in rows
            if int(row[0]) >= start_ms
        ]


@dataclass(frozen=True)
class GatePublicClient(RegistrationFreeMarketClient):
    base_url: str = "https://api.gateio.ws"
    timeout: int = 15

    @staticmethod
    def contract(symbol: str) -> str:
        base = symbol.removesuffix("USDT")
        return f"{base}_USDT"

    def last_price(self, symbol: str) -> float:
        response = requests.get(
            f"{self.base_url}/api/v4/futures/usdt/tickers",
            params={"contract": self.contract(symbol)},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return float(response.json()[0]["last"])

    def klines(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor = end_ms // 1000
        while cursor > start_ms // 1000:
            response = requests.get(
                f"{self.base_url}/api/v4/futures/usdt/candlesticks",
                params={
                    "contract": self.contract(symbol),
                    "interval": interval,
                    "from": str(start_ms // 1000),
                    "to": str(cursor),
                    "limit": "2000",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
            if not data:
                break
            rows.extend(data)
            cursor = int(data[0]["t"])
        return [
            {
                "open_time": int(row["t"]) * 1000,
                "open": float(row["o"]),
                "high": float(row["h"]),
                "low": float(row["l"]),
                "close": float(row["c"]),
                "volume": float(row["v"]),
            }
            for row in rows
            if int(row["t"]) * 1000 >= start_ms
        ]


@dataclass(frozen=True)
class KuCoinFuturesPublicClient(RegistrationFreeMarketClient):
    base_url: str = "https://api-futures.kucoin.com"
    timeout: int = 15
    symbols: dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "symbols",
            {
                "BTCUSDT": "XBTUSDTM",
                "ETHUSDT": "ETHUSDTM",
                "SOLUSDT": "SOLUSDTM",
            },
        )

    def last_price(self, symbol: str) -> float:
        instrument = (self.symbols or {}).get(symbol, symbol + "M")
        response = requests.get(
            f"{self.base_url}/api/v1/ticker",
            params={"symbol": instrument},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return float(response.json()["data"]["last"])

    def klines(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
    ) -> list[dict[str, Any]]:
        instrument = (self.symbols or {}).get(symbol, symbol + "M")
        rows: list[dict[str, Any]] = []
        cursor = end_ms // 1000
        while cursor > start_ms // 1000:
            response = requests.get(
                f"{self.base_url}/api/v1/kline/query",
                params={
                    "symbol": instrument,
                    "granularity": interval,
                    "startAt": str(start_ms // 1000),
                    "endAt": str(cursor),
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()["data"]
            if not data:
                break
            rows.extend(data)
            cursor = int(data[0][0])
        return [
            {
                "open_time": int(row[0]) * 1000,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            }
            for row in rows
            if int(row[0]) * 1000 >= start_ms
        ]


def client_for(venue: str) -> RegistrationFreeMarketClient:
    if venue == "okx":
        return OkxPublicClient()
    if venue == "gate":
        return GatePublicClient()
    if venue == "kucoin":
        return KuCoinFuturesPublicClient()
    raise ValueError(f"unsupported venue: {venue}")
