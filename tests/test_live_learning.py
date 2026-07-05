from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.live_learning import (
    apply_live_credit_to_candidate,
    build_trade_records_from_user_trades,
    live_credit_multiplier,
    recovered_score,
    score_records,
)


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
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    records = [
        {"symbol": "LABUSDT", "direction": "SHORT", "open_time": now - 300_000, "close_time": now - 180_000, "open_notional": 90, "net_pnl": 2.5, "commission": 0.08, "hold_seconds": 120},
        {"symbol": "LABUSDT", "direction": "SHORT", "open_time": now - 120_000, "close_time": now - 60_000, "open_notional": 80, "net_pnl": 1.0, "commission": 0.06, "hold_seconds": 60},
        {"symbol": "TLMUSDT", "direction": "LONG", "open_time": now - 120_000, "close_time": now - 69_000, "open_notional": 95, "net_pnl": -6.0, "commission": 0.09, "hold_seconds": 51},
    ]

    lab = score_records(records[:2], {})
    tlm = score_records(records[2:], {})

    assert lab["score"] > 65
    assert lab["status"] in {"normal", "strong"}
    assert tlm["score"] < 35
    assert tlm["status"] in {"weak", "penalty"}
    assert tlm["penalty_until"]


def test_score_records_applies_time_decay_to_recent_trades():
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    recent_win = [
        {"symbol": "LABUSDT", "direction": "SHORT", "open_time": now - 300_000, "close_time": now - 60_000, "open_notional": 90, "net_pnl": 1.0, "commission": 0.01, "hold_seconds": 120},
    ]
    old_win = [
        {"symbol": "LABUSDT", "direction": "SHORT", "open_time": now - 30 * 3600_000, "close_time": now - 29 * 3600_000, "open_notional": 90, "net_pnl": 1.0, "commission": 0.01, "hold_seconds": 120},
    ]
    config = {
        "live_credit_time_decay_enabled": True,
        "live_credit_recent_3h_multiplier": 1.5,
        "live_credit_old_multiplier": 0.5,
        "live_credit_recovery_enabled": False,
    }

    assert score_records(recent_win, config)["score"] > score_records(old_win, config)["score"]


def test_live_credit_uses_linear_multiplier_instead_of_blocking(monkeypatch):
    penalty_until = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(microsecond=0).isoformat()

    monkeypatch.setattr(
        "app.live_learning.live_score_for",
        lambda symbol, direction, config: {
            "enabled": True,
            "score": 21,
            "status": "penalty",
            "status_label": "惩罚区",
            "penalty_until": penalty_until,
            "consecutive_wins": 0,
            "consecutive_losses": 2,
            "notes": ["快速止损重罚"],
        },
    )

    candidate = apply_live_credit_to_candidate(
        {"symbol": "LABUSDT", "direction": "SHORT", "score": 120, "passed": True, "risk_pct": 15, "decision_reason": "原始信号通过"},
        {"live_credit_enabled": True},
    )

    assert candidate["passed"] is True
    assert candidate["risk_pct"] == 15 * 0.25
    assert candidate["live_credit_adjustment"]["risk_multiplier"] == 0.25
    assert "冷却倍率上限" in "；".join(candidate["live_credit_adjustment"]["reasons"])


def test_live_credit_multiplier_is_score_divided_by_50():
    config = {"live_credit_multiplier_divisor": 50, "live_credit_max_risk_multiplier": 2.0, "live_credit_fuse_score": 2}

    assert live_credit_multiplier({"score": 50, "penalty_until": None}, config) == 1.0
    assert live_credit_multiplier({"score": 75, "penalty_until": None}, config) == 1.5
    assert live_credit_multiplier({"score": 100, "penalty_until": None}, config) == 2.0
    assert live_credit_multiplier({"score": 1.5, "penalty_until": None}, config) == 0.0


def test_quick_loss_cooldown_caps_risk_and_marks_bypass(monkeypatch):
    penalty_until = (datetime.now(timezone.utc) + timedelta(minutes=30)).replace(microsecond=0).isoformat()

    monkeypatch.setattr(
        "app.live_learning.live_score_for",
        lambda symbol, direction, config: {
            "enabled": True,
            "score": 50,
            "status": "observe",
            "status_label": "observe",
            "penalty_until": penalty_until,
            "consecutive_wins": 0,
            "consecutive_losses": 1,
            "last_hold_seconds": 30,
            "notes": ["quick stop"],
        },
    )
    config = {
        "live_credit_enabled": True,
        "live_credit_multiplier_divisor": 50,
        "live_credit_max_risk_multiplier": 2.0,
        "live_credit_fuse_score": 2,
        "live_credit_quick_stop_seconds": 60,
        "live_credit_quick_loss_cooldown_cap": 0.4,
        "live_credit_cooldown_bypass_enabled": True,
        "live_credit_cooldown_bypass_min_score": 95,
        "live_credit_cooldown_bypass_min_cost_ratio": 18,
    }

    candidate = apply_live_credit_to_candidate(
        {"symbol": "LABUSDT", "direction": "LONG", "score": 120, "cost_ratio": 21, "passed": True, "risk_pct": 18},
        config,
    )

    assert candidate["risk_pct"] == 18 * 0.4
    assert candidate["live_credit_adjustment"]["risk_multiplier"] == 0.4
    assert candidate["live_credit_adjustment"]["cooldown"]["kind"] == "quick_loss"
    assert candidate["live_credit_adjustment"]["cooldown_bypass"] is True


def test_live_credit_natural_recovery_caps_at_default():
    old = int((datetime.now(timezone.utc) - timedelta(hours=24)).timestamp() * 1000)

    recovered = recovered_score(
        21,
        old,
        {
            "live_credit_recovery_enabled": True,
            "live_credit_recovery_interval_hours": 6,
            "live_credit_recovery_points": 3,
            "live_credit_recovery_cap": 50,
            "live_credit_fuse_score": 2,
        },
    )

    assert recovered["score"] == 33
    assert recovered["recovery_points"] == 12
