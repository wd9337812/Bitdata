from __future__ import annotations

from app.position_sizing import effective_position_risk
from app.protection import build_protection_plan
from app.protection_audit import audit_position_protection, enrich_positions_with_prices


class DummyClient:
    def __init__(self, algo_orders=None):
        self.algo_orders = algo_orders or []
        self.placed = []
        self.cancelled = []

    def open_algo_orders(self, symbol=None):
        return self.algo_orders

    def exchange_info(self):
        return {
            "symbols": [
                {
                    "symbol": "TESTUSDT",
                    "filters": [
                        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }
            ]
        }

    def position_side_dual(self):
        return {"dualSidePosition": True}

    def place_algo_order(self, **kwargs):
        self.placed.append(kwargs)
        return {"ok": True, **kwargs}

    def cancel_all_open_algo_orders(self, symbol):
        self.cancelled.append(symbol)
        return {"cancelled": symbol}


def test_enrich_positions_uses_position_risk_mark_price():
    positions = [{"symbol": "TESTUSDT", "positionAmt": "1", "entryPrice": "100", "positionSide": "LONG"}]
    risk = [{"symbol": "TESTUSDT", "positionAmt": "1", "positionSide": "LONG", "markPrice": "101.5"}]

    enriched = enrich_positions_with_prices(positions, risk)

    assert enriched[0]["markPrice"] == "101.5"
    assert enriched[0]["price_source"] == "positionRisk"


def test_audit_repairs_missing_stop_and_take_profit():
    client = DummyClient()
    position = {"symbol": "TESTUSDT", "positionAmt": "1", "entryPrice": "100", "markPrice": "100", "positionSide": "LONG"}

    result = audit_position_protection(client, position, {"protection_audit_auto_repair_enabled": True})

    assert result["repair_status"] == "repaired"
    assert {order["order_type"] for order in client.placed} == {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
    assert all(order["side"] == "SELL" for order in client.placed)


def test_audit_keeps_existing_valid_protection_orders():
    client = DummyClient(
        [
            {
                "symbol": "TESTUSDT",
                "side": "SELL",
                "orderType": "STOP_MARKET",
                "algoStatus": "NEW",
                "triggerPrice": "99",
                "positionSide": "LONG",
            },
            {
                "symbol": "TESTUSDT",
                "side": "SELL",
                "orderType": "TAKE_PROFIT_MARKET",
                "algoStatus": "NEW",
                "triggerPrice": "102",
                "positionSide": "LONG",
            },
        ]
    )
    position = {"symbol": "TESTUSDT", "positionAmt": "1", "entryPrice": "100", "markPrice": "100", "positionSide": "LONG"}

    result = audit_position_protection(client, position, {}, repair=True)

    assert result["protected"] is True
    assert client.placed == []


def test_audit_rebuilds_invalid_protection_orders():
    client = DummyClient(
        [
            {
                "symbol": "TESTUSDT",
                "side": "SELL",
                "orderType": "STOP_MARKET",
                "algoStatus": "NEW",
                "triggerPrice": "101",
                "positionSide": "LONG",
            }
        ]
    )
    position = {"symbol": "TESTUSDT", "positionAmt": "1", "entryPrice": "100", "markPrice": "100", "positionSide": "LONG"}

    result = audit_position_protection(client, position, {"protection_audit_rebuild_invalid_enabled": True})

    assert client.cancelled == ["TESTUSDT"]
    assert result["repair_status"] == "repaired"
    assert len(client.placed) == 2


def test_extreme_scalp_increases_top_quality_risk_with_cap():
    sizing = effective_position_risk(
        candidate_risk_pct=12,
        candidate={
            "mode": "extreme_sprint",
            "entry_type": "standard",
            "score": 150,
            "cost_ratio": 18,
            "symbol_quality": {"score": 82},
            "depth": {"depth_notional": 50_000},
        },
        guard={"risk_multiplier": 1.0},
        target={"effective_risk_multiplier": 1.0},
        config={
            "effective_position_sizing_enabled": True,
            "extreme_scalp_enabled": True,
            "extreme_scalp_super_score": 145,
            "extreme_scalp_super_risk_multiplier": 1.55,
            "extreme_scalp_max_risk_pct": 18,
            "effective_top_max_risk_pct": 14,
        },
        mode="extreme_sprint",
    )

    assert sizing["scalp_tier"] == "super"
    assert sizing["final_risk_pct"] == 18


def test_extreme_scalp_protection_profile_is_fast():
    signal = {
        "signal": "LONG",
        "last_price": 100,
        "atr": 2,
        "stop": 98,
        "take_profit": 103,
        "protection_profile": {"stop_atr": 0.55, "take_profit_atr": 0.75, "max_hold_bars": 2},
    }

    plan = build_protection_plan(signal, {"dynamic_protection_enabled": True}, entry_type="extreme_scalp", direction="LONG")

    assert plan["initial_stop"] == 98.9
    assert plan["initial_take_profit"] == 101.5
    assert plan["max_hold_bars"] == 2
