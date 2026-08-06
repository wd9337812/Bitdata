from __future__ import annotations

import json
from datetime import datetime, timezone

from app.s0_event_engine import build_s0_event_decision
from app.runtime_protection import _replace_dynamic_stop


def _config(**overrides: object) -> dict:
    config = {
        "s0_event_live_enabled": True,
        "s0_event_s_grade_min_score": 82.0,
        "s0_event_s_grade_max_account_risk_pct": 50.0,
        "s0_event_s_grade_max_leverage": 15,
        "s0_event_margin_pct": 97.0,
        "s0_event_stop_pct": 3.0,
        "s0_event_take_profit_r": 2.0,
        "s0_event_max_hold_seconds": 21600,
        "s0_event_max_age_seconds": 300,
        "s0_event_hard_stop_reserve_usdt": 0.25,
        "s0_event_min_liquidation_buffer_pct": 2.0,
        "hard_stop_equity": 5.0,
        "taker_fee_pct_round_trip": 0.08,
        "estimated_slippage_pct": 0.03,
    }
    return {**config, **overrides}


def _event(ts: int) -> dict:
    return {
        "type": "polymarket_binance_confirmed",
        "event_id": "poly:test:1",
        "symbol": "BTCUSDT",
        "direction": 1,
        "entry_price": 100000.0,
        "probability_delta": 0.09,
        "binance_confirmation_count": 2,
        "volume_z": 2.0,
        "move_pct": 0.25,
        "ts": ts,
    }


def test_confirmed_event_opens_with_hard_stop_headroom(monkeypatch, tmp_path) -> None:
    now = datetime.now(timezone.utc)
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"latest_events": [_event(int(now.timestamp() * 1000))]}),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.s0_event_engine.event_state_path", lambda: path)

    decision = build_s0_event_decision(
        _config(), {}, {"equity": 13.5, "available_balance": 13.5, "positions": []}, now
    )

    assert decision["action"] == "OPEN_LONG"
    assert decision["risk_pct"] <= 50.0
    assert decision["risk_pct"] <= (13.5 - 5.0 - 0.25) / 13.5 * 100
    assert decision["leverage"] == 15
    assert decision["signal"]["stop"] < decision["signal"]["last_price"]
    assert decision["signal"]["take_profit"] > decision["signal"]["last_price"]


def test_event_engine_rejects_unconfirmed_source(monkeypatch, tmp_path) -> None:
    now = datetime.now(timezone.utc)
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"latest_events": [{**_event(int(now.timestamp() * 1000)), "type": "volume_breakout"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.s0_event_engine.event_state_path", lambda: path)

    decision = build_s0_event_decision(
        _config(), {}, {"equity": 13.5, "available_balance": 13.5, "positions": []}, now
    )

    assert decision["action"] == "WAIT"
    assert decision["reason"] == "s0_event_no_s_grade"


def test_event_engine_rejects_duplicate_and_insufficient_buffer(monkeypatch, tmp_path) -> None:
    now = datetime.now(timezone.utc)
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"latest_events": [_event(int(now.timestamp() * 1000))]}),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.s0_event_engine.event_state_path", lambda: path)
    account = {"equity": 13.5, "available_balance": 13.5, "positions": []}

    duplicate = build_s0_event_decision(_config(), {"s0_event_last_traded_id": "poly:test:1"}, account, now)
    assert duplicate["reason"] == "s0_event_already_traded"

    cooldown = build_s0_event_decision(
        _config(), {"s0_event_recent_market_ids": {"poly:test:1": now.timestamp()}}, account, now
    )
    assert cooldown["reason"] == "s0_event_source_cooldown"

    buffer = build_s0_event_decision(
        _config(s0_event_s_grade_max_leverage=20, s0_event_min_liquidation_buffer_pct=3.0), {}, account, now
    )
    assert buffer["reason"] == "s0_event_liquidation_buffer_too_small"


def test_percent_trailing_stop_does_not_require_atr(monkeypatch) -> None:
    class Filters:
        @staticmethod
        def price(symbol: str, price: float) -> float:
            return price

    class Client:
        orders: list[dict] = []

        @staticmethod
        def open_algo_orders(symbol: str) -> list[dict]:
            return Client.orders

        @staticmethod
        def place_algo_order(**kwargs: object) -> dict:
            Client.orders.append(
                {
                    "algoType": "STOP_MARKET",
                    "triggerPrice": str(kwargs["trigger_price"]),
                    "algoId": 1,
                    "closePosition": True,
                }
            )
            return Client.orders[-1]

    action = {
        "action": "trail_stop",
        "direction": "LONG",
        "entry": 100.0,
        "mark": 110.0,
        "atr_pct": 0.0,
        "trailing_distance_pct": 3.0,
    }
    result = _replace_dynamic_stop(
        Client(), Filters(), {"symbol": "BTCUSDT"}, action, {}, {}
    )
    assert result["management_status"] != "price_or_atr_missing"
