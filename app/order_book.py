from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


class OrderBookGap(RuntimeError):
    pass


def _levels(rows: list[list[Any]] | None) -> dict[float, float]:
    result: dict[float, float] = {}
    for row in rows or []:
        if len(row) < 2:
            continue
        price = float(row[0])
        quantity = float(row[1])
        if price > 0 and quantity > 0:
            result[price] = quantity
    return result


@dataclass
class LocalOrderBook:
    symbol: str
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    last_update_id: int = 0
    initialized: bool = False
    started: bool = False

    def seed(self, snapshot: dict[str, Any]) -> None:
        self.bids = _levels(snapshot.get("bids"))
        self.asks = _levels(snapshot.get("asks"))
        self.last_update_id = int(snapshot.get("lastUpdateId") or 0)
        self.initialized = self.last_update_id > 0
        self.started = False

    def apply(self, event: dict[str, Any]) -> bool:
        if not self.initialized:
            return False
        first_id = int(event.get("U") or 0)
        final_id = int(event.get("u") or 0)
        previous_id = int(event.get("pu") or 0)
        if final_id < self.last_update_id:
            return False
        if not self.started:
            if not (first_id <= self.last_update_id <= final_id):
                return False
            self.started = True
        elif previous_id != self.last_update_id:
            raise OrderBookGap(f"{self.symbol} order book gap: pu={previous_id}, expected={self.last_update_id}")
        for target, rows in ((self.bids, event.get("b")), (self.asks, event.get("a"))):
            for row in rows or []:
                if len(row) < 2:
                    continue
                price = float(row[0])
                quantity = float(row[1])
                if quantity == 0:
                    target.pop(price, None)
                elif price > 0:
                    target[price] = quantity
        self.last_update_id = final_id
        return True

    def snapshot(self, levels: int = 20) -> dict[str, Any]:
        bids = [[str(price), str(self.bids[price])] for price in sorted(self.bids, reverse=True)[:levels]]
        asks = [[str(price), str(self.asks[price])] for price in sorted(self.asks)[:levels]]
        return {
            "symbol": self.symbol,
            "lastUpdateId": self.last_update_id,
            "bids": bids,
            "asks": asks,
        }


class OrderBookRegistry:
    def __init__(self) -> None:
        self._books: dict[str, LocalOrderBook] = {}
        self._lock = threading.RLock()

    def seed(self, symbol: str, snapshot: dict[str, Any]) -> None:
        with self._lock:
            book = self._books.setdefault(symbol.upper(), LocalOrderBook(symbol.upper()))
            book.seed(snapshot)

    def apply(self, symbol: str, event: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            book = self._books.get(symbol.upper())
            if book is None or not book.apply(event):
                return None
            return book.snapshot()

    def ready_symbols(self) -> list[str]:
        with self._lock:
            return [symbol for symbol, book in self._books.items() if book.initialized]


ORDER_BOOKS = OrderBookRegistry()
