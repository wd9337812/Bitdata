from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_UP
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


def ceil_step(value: float, step: str) -> float:
    decimal_value = Decimal(str(value))
    decimal_step = Decimal(step)
    if decimal_step <= 0:
        return float(decimal_value)
    rounded = (decimal_value / decimal_step).to_integral_value(rounding=ROUND_UP) * decimal_step
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

    def min_quantity_for_notional(self, symbol: str, entry_price: float, buffer_pct: float = 0.0) -> float:
        if entry_price <= 0:
            return 0.0
        filters = self.filters_for(symbol)
        lot = filters.get("LOT_SIZE", {})
        step = lot.get("stepSize", "0.001")
        min_qty = float(lot.get("minQty", 0) or 0)
        min_notional_qty = self.min_notional(symbol) * (1 + max(float(buffer_pct), 0.0) / 100) / entry_price
        return ceil_step(max(min_qty, min_notional_qty), step)

    def price(self, symbol: str, price: float) -> float:
        filters = self.filters_for(symbol)
        price_filter = filters.get("PRICE_FILTER", {})
        return round_step(price, price_filter.get("tickSize", "0.01"))

    def min_notional(self, symbol: str) -> float:
        filters = self.filters_for(symbol)
        notional = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL") or {}
        return float(notional.get("notional", notional.get("minNotional", 0)))
