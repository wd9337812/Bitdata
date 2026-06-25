from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import Any


def _decimal_places(step: str) -> int:
    value = Decimal(step).normalize()
    return max(0, -value.as_tuple().exponent)


def round_step(value: float, step: str) -> float:
    decimal_value = Decimal(str(value))
    decimal_step = Decimal(step)
    if decimal_step <= 0:
        return float(decimal_value)
    rounded = (decimal_value / decimal_step).to_integral_value(rounding=ROUND_DOWN) * decimal_step
    return float(round(rounded, _decimal_places(step)))


class ExchangeFilters:
    def __init__(self, exchange_info: dict[str, Any]) -> None:
        self.symbols = {item["symbol"]: item for item in exchange_info.get("symbols", [])}

    def filters_for(self, symbol: str) -> dict[str, Any]:
        info = self.symbols[symbol.upper()]
        return {item["filterType"]: item for item in info.get("filters", [])}

    def quantity(self, symbol: str, quantity: float) -> float:
        filters = self.filters_for(symbol)
        lot = filters.get("LOT_SIZE", {})
        return round_step(quantity, lot.get("stepSize", "0.001"))

    def price(self, symbol: str, price: float) -> float:
        filters = self.filters_for(symbol)
        price_filter = filters.get("PRICE_FILTER", {})
        return round_step(price, price_filter.get("tickSize", "0.01"))

    def min_notional(self, symbol: str) -> float:
        filters = self.filters_for(symbol)
        notional = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL") or {}
        return float(notional.get("notional", notional.get("minNotional", 0)))
