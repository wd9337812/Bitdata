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


def test_yolo_orderbook_scalp_uses_experiment_credit_with_legacy_soft_discount(monkeypatch):
    penalty_until = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(microsecond=0).isoformat()

    monkeypatch.setattr(
        "app.live_learning.live_score_for",
        lambda symbol, direction, config: {
            "enabled": True,
            "score": 12,
            "status": "penalty",
            "status_label": "penalty",
            "penalty_until": penalty_until,
            "closed_trades": 3,
            "wins": 0,
            "losses": 3,
            "consecutive_wins": 0,
            "consecutive_losses": 3,
            "commission": 0.2,
            "net_pnl": -5,
            "profit_factor": 0,
            "notes": ["legacy loss"],
        },
    )

    monkeypatch.setattr(
        "app.live_learning.strategy_live_score_for",
        lambda symbol, direction, strategy_family, config: {
            "enabled": True,
            "score": 50,
            "status": "new",
            "status_label": "剥头皮新策略观察",
            "strategy_family": strategy_family,
            "closed_trades": 0,
            "wins": 0,
            "losses": 0,
            "consecutive_wins": 0,
            "consecutive_losses": 0,
            "penalty_until": None,
            "commission": 0,
            "net_pnl": 0,
            "profit_factor": 0,
            "notes": [],
        },
    )

    candidate = apply_live_credit_to_candidate(
        {
            "symbol": "LABUSDT",
            "direction": "SHORT",
            "mode": "yolo_scalp",
            "entry_type": "orderbook_impact",
            "score": 100,
            "passed": True,
            "risk_pct": 50,
        },
        {
            "live_credit_enabled": True,
            "live_credit_multiplier_divisor": 50,
            "live_credit_max_risk_multiplier": 2.0,
            "live_credit_fuse_score": 2,
            "yolo_scalp_credit_experiment_enabled": True,
            "yolo_scalp_strategy_credit_enabled": True,
            "yolo_scalp_legacy_credit_soft_multiplier": 0.70,
            "yolo_scalp_legacy_credit_soft_penalty_multiplier": 0.70,
        },
    )

    assert candidate["passed"] is True
    assert candidate["risk_pct"] == 50
    assert candidate["live_credit"]["status"] == "new"
    assert candidate["live_credit"]["strategy_family"] == "orderbook_scalp"
    assert candidate["legacy_live_credit"]["score"] == 12
    assert candidate["live_credit_adjustment"]["strategy_family"] == "orderbook_scalp"
    assert candidate["live_credit_adjustment"]["legacy_soft_multiplier"] == 1.0
    assert any("旧策略信用仅展示" in item for item in candidate["live_credit_adjustment"]["reasons"])


def test_extreme_v2_uses_its_own_strategy_credit(monkeypatch):
    monkeypatch.setattr(
        "app.live_learning.live_score_for",
        lambda symbol, direction, config: {
            "enabled": True,
            "score": 10,
            "status": "penalty",
            "status_label": "旧策略惩罚",
            "closed_trades": 3,
            "wins": 0,
            "losses": 3,
            "consecutive_wins": 0,
            "consecutive_losses": 3,
            "penalty_until": None,
            "commission": 1,
            "net_pnl": -5,
            "profit_factor": 0,
            "notes": [],
        },
    )
    monkeypatch.setattr(
        "app.live_learning.strategy_live_score_for",
        lambda symbol, direction, strategy_family, config: {
            "enabled": True,
            "score": 50,
            "status": "new",
            "status_label": "极限 V2 新策略观察",
            "strategy_family": strategy_family,
            "closed_trades": 0,
            "wins": 0,
            "losses": 0,
            "consecutive_wins": 0,
            "consecutive_losses": 0,
            "penalty_until": None,
            "commission": 0,
            "net_pnl": 0,
            "profit_factor": 0,
            "notes": [],
        },
    )

    candidate = apply_live_credit_to_candidate(
        {"symbol": "SOLUSDT", "direction": "LONG", "mode": "extreme_sprint", "entry_type": "standard", "score": 100, "passed": True, "risk_pct": 10},
        {"live_credit_enabled": True, "strategy_family_credit_enabled": True},
    )

    assert candidate["passed"] is True
    assert candidate["risk_pct"] == 10
    assert candidate["live_credit"]["strategy_family"] == "extreme_v2_roll"
    assert candidate["legacy_live_credit"]["score"] == 10
    assert any("旧策略信用仅展示" in item for item in candidate["live_credit_adjustment"]["reasons"])


def test_v4_uses_only_v4_strategy_credit_for_position_size(monkeypatch):
    monkeypatch.setattr(
        "app.live_learning.live_score_for",
        lambda symbol, direction, config: {
            "enabled": True,
            "score": 10,
            "status": "penalty",
            "status_label": "legacy penalty",
            "closed_trades": 4,
            "wins": 0,
            "losses": 4,
            "consecutive_wins": 0,
            "consecutive_losses": 4,
            "penalty_until": None,
            "commission": 1,
            "net_pnl": -5,
            "profit_factor": 0,
            "notes": [],
        },
    )
    monkeypatch.setattr(
        "app.live_learning.strategy_live_score_for",
        lambda symbol, direction, strategy_family, config: {
            "enabled": True,
            "score": 50,
            "status": "new",
            "status_label": "V4 new strategy observation",
            "strategy_family": strategy_family,
            "closed_trades": 0,
            "wins": 0,
            "losses": 0,
            "consecutive_wins": 0,
            "consecutive_losses": 0,
            "penalty_until": None,
            "commission": 0,
            "net_pnl": 0,
            "profit_factor": 0,
            "notes": [],
        },
    )

    candidate = apply_live_credit_to_candidate(
        {
            "symbol": "SOLUSDT",
            "direction": "LONG",
            "mode": "extreme_sprint",
            "entry_type": "v3_breakout",
            "strategy_family": "extreme_v4_roll",
            "strategy_generation": "v4",
            "score": 100,
            "passed": True,
            "risk_pct": 5,
        },
        {
            "live_credit_enabled": True,
            "strategy_family_credit_enabled": True,
            "v4_credit_score_weight": 0.25,
            "v4_credit_min_multiplier": 0.60,
            "v4_credit_max_multiplier": 1.20,
        },
    )

    assert candidate["passed"] is True
    assert candidate["risk_pct"] == 5
    assert candidate["live_credit"]["strategy_family"] == "extreme_v4_roll"
    assert candidate["legacy_live_credit"]["score"] == 10
    assert candidate["live_credit_adjustment"]["strategy_family"] == "extreme_v4_roll"
    assert candidate["live_credit_adjustment"]["risk_multiplier"] == 1.0


def test_live_credit_multiplier_is_score_divided_by_50():
    config = {"live_credit_multiplier_divisor": 50, "live_credit_max_risk_multiplier": 2.0, "live_credit_fuse_score": 2}

    assert live_credit_multiplier({"score": 50, "penalty_until": None}, config) == 1.0
    assert live_credit_multiplier({"score": 75, "penalty_until": None}, config) == 1.5
    assert live_credit_multiplier({"score": 100, "penalty_until": None}, config) == 2.0
    assert live_credit_multiplier({"score": 1.5, "penalty_until": None}, config) == 0.0


def test_live_credit_blocks_boost_until_profitability_is_proven(monkeypatch):
    monkeypatch.setattr(
        "app.live_learning.live_score_for",
        lambda symbol, direction, config: {
            "enabled": True,
            "score": 80,
            "status": "strong",
            "status_label": "strong",
            "closed_trades": 2,
            "wins": 1,
            "losses": 1,
            "consecutive_wins": 0,
            "consecutive_losses": 0,
            "penalty_until": None,
            "commission": 0.1,
            "net_pnl": -0.1,
            "profit_factor": 0.8,
            "notes": [],
        },
    )

    candidate = apply_live_credit_to_candidate(
        {"symbol": "TESTUSDT", "direction": "LONG", "score": 90, "passed": True, "risk_pct": 10},
        {
            "live_credit_enabled": True,
            "live_credit_multiplier_divisor": 50,
            "live_credit_max_risk_multiplier": 2.0,
            "live_credit_unqualified_boost_cap": 1.0,
        },
    )

    assert candidate["risk_pct"] == 7.5
    assert candidate["live_credit_adjustment"]["risk_multiplier"] == 0.75
    assert candidate["live_credit_adjustment"]["boost_qualified"] is False


def test_live_credit_allows_extra_boost_after_profitable_streak(monkeypatch):
    monkeypatch.setattr(
        "app.live_learning.live_score_for",
        lambda symbol, direction, config: {
            "enabled": True,
            "score": 90,
            "status": "strong",
            "status_label": "strong",
            "closed_trades": 4,
            "wins": 4,
            "losses": 0,
            "consecutive_wins": 3,
            "consecutive_losses": 0,
            "penalty_until": None,
            "commission": 0.1,
            "net_pnl": 2.0,
            "profit_factor": 3.0,
            "notes": [],
        },
    )

    candidate = apply_live_credit_to_candidate(
        {"symbol": "WINUSDT", "direction": "SHORT", "score": 90, "passed": True, "risk_pct": 10},
        {
            "live_credit_enabled": True,
            "live_credit_multiplier_divisor": 50,
            "live_credit_max_risk_multiplier": 2.0,
            "live_credit_tail_win_count": 3,
            "live_credit_streak_profit_multiplier": 1.15,
        },
    )

    assert candidate["risk_pct"] == 20
    assert candidate["live_credit_adjustment"]["boost_qualified"] is True
    assert any("连续盈利且净收益/PF达标" in item for item in candidate["live_credit_adjustment"]["reasons"])


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


def test_extreme_probe_new_symbol_gets_small_risk_cap(monkeypatch):
    monkeypatch.setattr(
        "app.live_learning.live_score_for",
        lambda symbol, direction, config: {
            "enabled": True,
            "score": 50,
            "status": "new",
            "status_label": "new",
            "closed_trades": 0,
            "wins": 0,
            "losses": 0,
            "consecutive_wins": 0,
            "consecutive_losses": 0,
            "penalty_until": None,
            "commission": 0,
            "net_pnl": 0,
            "notes": [],
        },
    )

    candidate = apply_live_credit_to_candidate(
        {"symbol": "NEWUSDT", "direction": "LONG", "entry_type": "extreme_probe", "score": 100, "passed": True, "risk_pct": 6},
        {
            "live_credit_enabled": True,
            "live_credit_multiplier_divisor": 50,
            "live_credit_max_risk_multiplier": 2.0,
            "live_credit_fuse_score": 2,
            "extreme_probe_new_symbol_max_risk_pct": 2.2,
        },
    )

    assert candidate["risk_pct"] == 2.2
    assert any("new probe cap" in item for item in candidate["live_credit_adjustment"]["reasons"])


def test_extreme_probe_after_loss_gets_extra_damping(monkeypatch):
    penalty_until = (datetime.now(timezone.utc) + timedelta(minutes=30)).replace(microsecond=0).isoformat()
    monkeypatch.setattr(
        "app.live_learning.live_score_for",
        lambda symbol, direction, config: {
            "enabled": True,
            "score": 45,
            "status": "weak",
            "status_label": "weak",
            "closed_trades": 1,
            "wins": 0,
            "losses": 1,
            "consecutive_wins": 0,
            "consecutive_losses": 1,
            "last_hold_seconds": 120,
            "penalty_until": penalty_until,
            "commission": 0.2,
            "net_pnl": -0.5,
            "notes": [],
        },
    )

    candidate = apply_live_credit_to_candidate(
        {"symbol": "LOSSUSDT", "direction": "SHORT", "entry_type": "extreme_probe", "score": 100, "passed": True, "risk_pct": 6},
        {
            "live_credit_enabled": True,
            "live_credit_multiplier_divisor": 50,
            "live_credit_max_risk_multiplier": 2.0,
            "live_credit_fuse_score": 2,
            "live_credit_loss_cooldown_cap": 0.60,
            "live_credit_fee_pressure_enabled": True,
            "live_credit_fee_pressure_ratio": 0.20,
            "live_credit_fee_pressure_risk_multiplier": 0.75,
            "extreme_probe_loss_risk_multiplier": 0.55,
            "extreme_probe_after_loss_max_risk_pct": 1.2,
        },
    )

    assert candidate["risk_pct"] <= 1.2
    assert candidate["score"] < 100
    assert any("probe after loss" in item for item in candidate["live_credit_adjustment"]["reasons"])


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
