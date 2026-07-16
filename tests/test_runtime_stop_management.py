from __future__ import annotations

from contextlib import contextmanager

import app.runtime_protection as runtime_protection
from app.runtime_protection import _replace_dynamic_stop, manage_runtime_protection


class Filters:
    def price(self, symbol, value):
        return round(float(value), 4)


class Client:
    def __init__(self):
        self.orders = [
            {
                "symbol": "TESTUSDT",
                "orderType": "STOP_MARKET",
                "triggerPrice": "98",
                "algoId": 1,
                "positionSide": "LONG",
            }
        ]
        self.events = []

    def open_algo_orders(self, symbol):
        self.events.append("query")
        return list(self.orders)

    def place_algo_order(self, **kwargs):
        self.events.append("place")
        order = {
            "symbol": kwargs["symbol"],
            "orderType": kwargs["order_type"],
            "triggerPrice": str(kwargs["trigger_price"]),
            "algoId": 2,
            "positionSide": kwargs.get("position_side"),
        }
        self.orders.append(order)
        return order

    def cancel_algo_order(self, algo_id):
        self.events.append(f"cancel:{algo_id}")
        self.orders = [order for order in self.orders if order.get("algoId") != algo_id]
        return {"algoId": algo_id}


def test_dynamic_stop_is_confirmed_before_old_stop_is_cancelled():
    client = Client()
    tracked = {"trailing_distance_atr": 0.5}
    result = _replace_dynamic_stop(
        client,
        Filters(),
        {"symbol": "TESTUSDT", "positionAmt": "1", "positionSide": "LONG"},
        {"symbol": "TESTUSDT", "direction": "LONG", "action": "trail_stop", "entry": 100, "mark": 105, "atr_pct": 2 / 105 * 100},
        tracked,
        {"runtime_stop_management_min_improvement_atr": 0.1},
    )

    assert result["executed"] is True
    assert result["desired_stop"] == 104.0
    assert client.events.index("place") < client.events.index("cancel:1")
    assert tracked["managed_stop"] == 104.0
    assert [order["algoId"] for order in client.orders] == [2]


def test_dynamic_stop_keeps_close_position_stop_when_atomic_replace_is_unavailable():
    client = Client()
    client.orders[0]["closePosition"] = True
    tracked = {"trailing_distance_atr": 0.5}

    result = _replace_dynamic_stop(
        client,
        Filters(),
        {"symbol": "TESTUSDT", "positionAmt": "1", "positionSide": "LONG"},
        {"symbol": "TESTUSDT", "direction": "LONG", "action": "trail_stop", "entry": 100, "mark": 105, "atr_pct": 2 / 105 * 100},
        tracked,
        {"runtime_stop_management_min_improvement_atr": 0.1},
    )

    assert result["executed"] is False
    assert result["management_status"] == "exchange_atomic_replace_unavailable"
    assert client.events == ["query"]
    assert [order["algoId"] for order in client.orders] == [1]


def test_runtime_protection_owns_critical_request_priority(monkeypatch):
    priorities = []

    @contextmanager
    def priority(name):
        priorities.append(name)
        yield

    monkeypatch.setattr(runtime_protection, "request_priority", priority)
    monkeypatch.setattr(runtime_protection, "_manage_runtime_protection", lambda *args, **kwargs: {"enabled": True})

    result = manage_runtime_protection(object(), {}, {}, {})

    assert result == {"enabled": True}
    assert priorities == ["critical"]
