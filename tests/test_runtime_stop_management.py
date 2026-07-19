from __future__ import annotations

from contextlib import contextmanager

import app.runtime_protection as runtime_protection
from app.runtime_protection import (
    _execute_v432_add_on,
    _replace_dynamic_stop,
    build_v432_add_on_plan,
    manage_runtime_protection,
)


class Filters:
    def price(self, symbol, value):
        return round(float(value), 4)

    def quantity(self, symbol, value):
        return round(float(value), 4)

    def min_notional(self, symbol):
        return 5.0


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
        self.next_algo_id = 2

    def open_algo_orders(self, symbol):
        self.events.append("query")
        return list(self.orders)

    def place_algo_order(self, **kwargs):
        self.events.append("place")
        order = {
            "symbol": kwargs["symbol"],
            "orderType": kwargs["order_type"],
            "triggerPrice": str(kwargs["trigger_price"]),
            "algoId": self.next_algo_id,
            "positionSide": kwargs.get("position_side"),
            "closePosition": kwargs.get("close_position", True),
        }
        self.next_algo_id += 1
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
    assert result["management_status"] == "exchange_atomic_replace_unavailable_hedge_mode"
    assert client.events == ["query"]
    assert [order["algoId"] for order in client.orders] == [1]


def test_dynamic_stop_uses_confirmed_reduce_only_bridge_in_one_way_mode():
    client = Client()
    client.orders[0]["closePosition"] = True
    client.orders[0]["positionSide"] = "BOTH"
    tracked = {"trailing_distance_atr": 0.5}

    result = _replace_dynamic_stop(
        client,
        Filters(),
        {"symbol": "TESTUSDT", "positionAmt": "1", "positionSide": "BOTH"},
        {"symbol": "TESTUSDT", "direction": "LONG", "action": "trail_stop", "entry": 100, "mark": 105, "atr_pct": 2 / 105 * 100},
        tracked,
        {"runtime_stop_management_min_improvement_atr": 0.1},
    )

    assert result["executed"] is True
    assert result["management_status"] == "stop_tightened_with_reduce_only_bridge"
    assert client.events.index("place") < client.events.index("cancel:1")
    assert [order["algoId"] for order in client.orders] == [3]
    assert client.orders[0]["closePosition"] is True


def test_v432_add_on_plan_caps_total_nominal_risk_and_quantity():
    plan = build_v432_add_on_plan(
        {
            "symbol": "TESTUSDT",
            "positionAmt": "10",
            "positionSide": "BOTH",
            "entryPrice": "100",
            "markPrice": "105",
        },
        {
            "direction": "LONG",
            "action": "move_break_even",
            "executed": True,
            "management_status": "stop_tightened_with_reduce_only_bridge",
            "mark": 105,
            "desired_stop": 100.1,
            "pnl_pct": 5.0,
            "atr_pct": 2.0,
        },
        {
            "strategy_version": "v4.3.2",
            "position_confidence": {"add_on_eligible": True},
            "initial_risk_pct": 7.5,
            "initial_quantity": 10,
            "leverage": 5,
        },
        {"equity": 100, "available_balance": 100},
        {
            "hard_stop_equity": 5,
            "effective_min_order_notional_usdt": 10,
            "opportunity_v432_add_on_total_risk_cap_pct": 15,
            "opportunity_v432_add_on_max_initial_quantity_ratio": 0.75,
        },
        Filters(),
        {"drawdown_pct": 0, "fallback_active": False},
    )

    assert plan["allowed"] is True
    assert plan["quantity"] <= 7.5
    assert plan["total_nominal_risk_pct"] <= 15.0


def test_v432_add_on_plan_accepts_confirmed_trailing_stop_after_fast_move():
    plan = build_v432_add_on_plan(
        {
            "symbol": "TESTUSDT",
            "positionAmt": "10",
            "positionSide": "BOTH",
            "entryPrice": "100",
            "markPrice": "105",
        },
        {
            "direction": "LONG",
            "action": "trail_stop",
            "executed": True,
            "management_status": "stop_tightened_with_reduce_only_bridge",
            "mark": 105,
            "desired_stop": 101,
            "pnl_pct": 5.0,
            "atr_pct": 2.0,
        },
        {
            "strategy_version": "v4.3.2",
            "position_confidence": {"add_on_eligible": True},
            "initial_risk_pct": 7.5,
            "initial_quantity": 10,
            "leverage": 5,
        },
        {"equity": 100, "available_balance": 100},
        {
            "hard_stop_equity": 5,
            "effective_min_order_notional_usdt": 10,
            "opportunity_v432_add_on_total_risk_cap_pct": 15,
            "opportunity_v432_add_on_max_initial_quantity_ratio": 0.75,
        },
        Filters(),
        {"drawdown_pct": 0, "fallback_active": False},
    )

    assert plan["allowed"] is True


def test_v432_failed_protection_rolls_back_only_actual_filled_quantity(monkeypatch):
    class AddOnClient:
        def __init__(self):
            self.market_orders = []

        def open_algo_orders(self, symbol):
            return [
                {
                    "symbol": symbol,
                    "orderType": "STOP_MARKET",
                    "triggerPrice": "100.1",
                    "closePosition": True,
                }
            ]

        def place_market_order(self, symbol, side, quantity, **kwargs):
            self.market_orders.append({"symbol": symbol, "side": side, "quantity": quantity, **kwargs})
            return {"symbol": symbol, "executedQty": "2.5"}

        def position_risk(self):
            return [{"symbol": "TESTUSDT", "positionAmt": "12.5", "positionSide": "BOTH", "markPrice": "105"}]

    monkeypatch.setattr(
        runtime_protection,
        "build_v432_add_on_plan",
        lambda *args, **kwargs: {
            "allowed": True,
            "symbol": "TESTUSDT",
            "direction": "LONG",
            "quantity": 7.5,
            "confirmed_stop": 100.1,
            "add_on_risk_pct": 4,
            "total_nominal_risk_pct": 11.5,
            "blockers": [],
        },
    )
    monkeypatch.setattr(runtime_protection, "enrich_positions_with_prices", lambda rows, _: rows)
    monkeypatch.setattr(
        runtime_protection,
        "audit_position_protection",
        lambda *args, **kwargs: {"protected": False, "repair_status": "failed"},
    )
    monkeypatch.setattr(runtime_protection, "record_event", lambda *args, **kwargs: None)
    client = AddOnClient()

    result = _execute_v432_add_on(
        client,
        Filters(),
        {"symbol": "TESTUSDT", "positionAmt": "10", "positionSide": "BOTH", "markPrice": "105"},
        {},
        {},
        {},
        {},
        {},
    )

    assert result["status"] == "protection_failed_add_on_rolled_back"
    assert result["filled_quantity"] == 2.5
    assert client.market_orders[0]["quantity"] == 7.5
    assert client.market_orders[1]["quantity"] == 2.5
    assert client.market_orders[1]["reduce_only"] is True


def test_v432_unconfirmed_fill_blocks_repeat_add_on(monkeypatch):
    class AddOnClient:
        def __init__(self):
            self.market_orders = []

        def open_algo_orders(self, symbol):
            return [
                {
                    "symbol": symbol,
                    "orderType": "STOP_MARKET",
                    "triggerPrice": "100.1",
                    "closePosition": True,
                }
            ]

        def place_market_order(self, symbol, side, quantity, **kwargs):
            self.market_orders.append({"symbol": symbol, "side": side, "quantity": quantity, **kwargs})
            return {"symbol": symbol}

        def position_risk(self):
            return [{"symbol": "TESTUSDT", "positionAmt": "10", "positionSide": "BOTH", "markPrice": "105"}]

    tracked = {
        "strategy_version": "v4.3.2",
        "position_confidence": {"add_on_eligible": True},
        "initial_risk_pct": 7.5,
        "initial_quantity": 10,
        "leverage": 5,
    }
    action = {
        "symbol": "TESTUSDT",
        "direction": "LONG",
        "action": "move_break_even",
        "executed": True,
        "management_status": "stop_tightened_confirmed",
        "desired_stop": 100.1,
        "entry": 100,
        "mark": 105,
        "pnl_pct": 5,
        "atr_pct": 2,
    }
    config = {
        "hard_stop_equity": 5,
        "effective_min_order_notional_usdt": 10,
        "opportunity_v432_add_on_total_risk_cap_pct": 15,
        "opportunity_v432_add_on_max_initial_quantity_ratio": 0.75,
    }
    monkeypatch.setattr(runtime_protection, "enrich_positions_with_prices", lambda rows, _: rows)
    monkeypatch.setattr(
        runtime_protection,
        "audit_position_protection",
        lambda *args, **kwargs: {"protected": True},
    )
    monkeypatch.setattr(runtime_protection, "record_event", lambda *args, **kwargs: None)
    client = AddOnClient()
    position = {"symbol": "TESTUSDT", "positionAmt": "10", "positionSide": "BOTH", "markPrice": "105"}

    account = {"equity": 100, "available_balance": 100}
    result = _execute_v432_add_on(client, Filters(), position, action, tracked, account, config, {})
    repeated = _execute_v432_add_on(client, Filters(), position, action, tracked, account, config, {})

    assert result["status"] == "add_on_fill_unconfirmed_repeat_blocked"
    assert result["executed"] is False
    assert tracked["add_on_executed"] is True
    assert tracked["add_on_fill_unconfirmed"] is True
    assert "add_on_already_executed" in repeated["blockers"]
    assert len(client.market_orders) == 1


def test_v432_add_on_persists_one_shot_guard_before_follow_up_query(monkeypatch):
    class AddOnClient:
        def open_algo_orders(self, symbol):
            return [
                {
                    "symbol": symbol,
                    "orderType": "STOP_MARKET",
                    "triggerPrice": "100.1",
                    "closePosition": True,
                }
            ]

        def place_market_order(self, symbol, side, quantity, **kwargs):
            return {"symbol": symbol, "executedQty": str(quantity)}

        def position_risk(self):
            raise TimeoutError("position query timed out")

    tracked = {
        "strategy_version": "v4.3.2",
        "position_confidence": {"add_on_eligible": True},
        "initial_risk_pct": 7.5,
        "initial_quantity": 10,
        "leverage": 5,
    }
    runtime_positions = {"TESTUSDT:LONG": tracked}
    saved = []
    monkeypatch.setattr(runtime_protection, "save_state", lambda payload: saved.append(payload))
    position = {"symbol": "TESTUSDT", "positionAmt": "10", "positionSide": "BOTH", "markPrice": "105"}
    action = {
        "symbol": "TESTUSDT",
        "direction": "LONG",
        "action": "move_break_even",
        "executed": True,
        "management_status": "stop_tightened_confirmed",
        "desired_stop": 100.1,
        "entry": 100,
        "mark": 105,
        "pnl_pct": 5,
        "atr_pct": 2,
    }
    config = {
        "hard_stop_equity": 5,
        "effective_min_order_notional_usdt": 10,
        "opportunity_v432_add_on_total_risk_cap_pct": 15,
        "opportunity_v432_add_on_max_initial_quantity_ratio": 0.75,
    }

    try:
        _execute_v432_add_on(
            AddOnClient(),
            Filters(),
            position,
            action,
            tracked,
            {"equity": 100, "available_balance": 100},
            config,
            {"runtime_protection_positions": runtime_positions},
        )
    except TimeoutError:
        pass
    else:
        raise AssertionError("follow-up query should fail in this test")

    assert tracked["add_on_attempted"] is True
    assert saved and saved[0]["runtime_protection_positions"] is runtime_positions
    repeated = build_v432_add_on_plan(
        position,
        action,
        tracked,
        {"equity": 100, "available_balance": 100},
        config,
        Filters(),
    )
    assert "add_on_already_executed" in repeated["blockers"]


def test_v432_add_on_plan_rejects_non_core_and_release_fallback():
    base = {
        "strategy_version": "v4.3.2",
        "position_confidence": {"add_on_eligible": False},
        "initial_risk_pct": 4,
        "initial_quantity": 10,
    }
    plan = build_v432_add_on_plan(
        {"symbol": "TESTUSDT", "positionAmt": "10", "positionSide": "BOTH", "markPrice": "105"},
        {
            "direction": "LONG",
            "action": "move_break_even",
            "executed": True,
            "management_status": "stop_tightened_with_reduce_only_bridge",
            "mark": 105,
            "desired_stop": 100.1,
            "pnl_pct": 5,
            "atr_pct": 2,
        },
        base,
        {"equity": 100, "available_balance": 100},
        {"hard_stop_equity": 5},
        Filters(),
        {"drawdown_pct": 8, "fallback_active": True},
    )

    assert plan["allowed"] is False
    assert "initial_opportunity_not_eligible" in plan["blockers"]
    assert "release_drawdown_fallback" in plan["blockers"]


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
