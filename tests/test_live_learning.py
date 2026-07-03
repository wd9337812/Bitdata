from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.live_learning import apply_live_credit_to_candidate, build_trade_records_from_user_trades, score_records


def test_trade_record_builder_groups_open_and_close_fills():
    trades = {
        "LABUSDT": [
            {"symbol": "LABUSDT", "positionSide": "SHORT", "side": "SELL", "qty": "2", "price": "10", "time": 1000, "commission": "0.01", "realizedPnl": "0"},
            {"symbol": "LABUSDT", "positionSide": "SHORT", "side": "SELL", "qty": "1", "price": "10", "time": 1000, "commission": "0.005", "realizedPnl": "0"},
            {"symbol": "LABUSDT", "positionSide": "SHORT", "side": "BUY", "qty": "3", "price": "9", "time": 61_000, "commission": "0.02", "realizedPnl": "3"},
        ]
    }

    records = build_trade_records_from_user_trades(trades)

    assert len(records) == 1
    assert records[0]["symbol"] == "LABUSDT"
    assert records[0]["direction"] == "SHORT"
    assert records[0]["realized_pnl"] == 3
    assert records[0]["net_pnl"] == 2.965
    assert records[0]["hold_seconds"] == 60


def test_score_records_rewards_winners_and_punishes_quick_losses():
    records = [
        {"symbol": "LABUSDT", "direction": "SHORT", "open_time": 1000, "close_time": 121_000, "open_notional": 90, "net_pnl": 2.5, "commission": 0.08, "hold_seconds": 120},
        {"symbol": "LABUSDT", "direction": "SHORT", "open_time": 200_000, "close_time": 260_000, "open_notional": 80, "net_pnl": 1.0, "commission": 0.06, "hold_seconds": 60},
        {"symbol": "TLMUSDT", "direction": "LONG", "open_time": 300_000, "close_time": 351_000, "open_notional": 95, "net_pnl": -6.0, "commission": 0.09, "hold_seconds": 51},
    ]

    lab = score_records(records[:2], {})
    tlm = score_records(records[2:], {})

    assert lab["score"] > 65
    assert lab["status"] in {"normal", "strong"}
    assert tlm["score"] < 35
    assert tlm["status"] in {"weak", "penalty"}
    assert tlm["penalty_until"]


def test_live_credit_penalty_blocks_candidate(monkeypatch):
    penalty_until = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(microsecond=0).isoformat()

    monkeypatch.setattr(
        "app.live_learning.live_score_for",
        lambda symbol, direction, config: {
            "enabled": True,
            "score": 20,
            "status": "penalty",
            "status_label": "惩罚区",
            "penalty_until": penalty_until,
            "consecutive_wins": 0,
            "consecutive_losses": 2,
            "notes": ["快速止损重罚"],
        },
    )

    candidate = apply_live_credit_to_candidate(
        {"symbol": "TLMUSDT", "direction": "LONG", "score": 120, "passed": True, "risk_pct": 15, "decision_reason": "原始信号通过"},
        {"live_credit_enabled": True},
    )

    assert candidate["passed"] is False
    assert candidate["risk_pct"] == 15
    assert candidate["reason"] in {"live_credit_cooldown", "live_credit_penalty"}
    assert candidate["live_credit_adjustment"]["risk_multiplier"] == 0
