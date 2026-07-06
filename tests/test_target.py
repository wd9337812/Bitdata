from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.position_sizing import explain_position_sizing
from app.target import stage_name_for_equity, target_progress, target_state_updates


def test_target_progress_behind_raises_display_multiplier_without_enabling_risk():
    now = datetime(2026, 7, 6, tzinfo=timezone.utc)
    progress = target_progress(
        {
            "target_controller_enabled": True,
            "target_risk_adjustment_enabled": False,
            "target_phase_a_equity": 10_000,
            "target_phase_days": 30,
        },
        {"target_phase_a_start_equity": 50, "target_phase_a_start_time": (now - timedelta(days=15)).isoformat()},
        {"equity": 100},
        now=now,
    )
    assert progress["status"] == "critical"
    assert progress["risk_multiplier"] > 1
    assert progress["effective_risk_multiplier"] == 1.0
    assert progress["required_daily_return_pct"] > 0


def test_target_progress_ahead_lowers_multiplier_when_enabled():
    now = datetime(2026, 7, 6, tzinfo=timezone.utc)
    progress = target_progress(
        {
            "target_controller_enabled": True,
            "target_risk_adjustment_enabled": True,
            "target_phase_a_equity": 10_000,
            "target_phase_days": 30,
            "target_progress_ahead_multiplier": 0.7,
        },
        {"target_phase_a_start_equity": 50, "target_phase_a_start_time": (now - timedelta(days=1)).isoformat()},
        {"equity": 1_000},
        now=now,
    )
    assert progress["status"] == "ahead"
    assert progress["effective_risk_multiplier"] == 0.7


def test_target_state_updates_initialize_active_phase():
    now = datetime(2026, 7, 6, tzinfo=timezone.utc)
    updates = target_state_updates({"target_controller_enabled": True, "target_phase_a_equity": 10_000}, {}, {"equity": 50}, now=now)
    assert updates["target_active_phase"] == "A"
    assert updates["target_phase_a_start_equity"] == 50
    assert updates["target_phase_a_start_time"] == now.isoformat()


def test_stage_name_for_equity():
    assert stage_name_for_equity(44).startswith("S0")
    assert stage_name_for_equity(10_000).startswith("S4")
    assert stage_name_for_equity(1_000_000).startswith("S6")


def test_position_sizing_explains_multipliers_and_caps():
    sizing = explain_position_sizing(
        base_risk_pct=28,
        candidate={
            "entry_type_label": "抢跑试探",
            "quality_risk_multiplier": 0.8,
            "live_credit_adjustment": {"risk_multiplier": 1.2, "reasons": ["实盘信用 60 分"]},
        },
        guard={"risk_multiplier": 0.5, "reason": "scaled"},
        target={"effective_risk_multiplier": 1.0, "reason": "进度接近目标曲线"},
        final_risk_pct=13.44,
        risk={"max_notional": 100, "max_margin": 20},
    )
    assert sizing["multipliers"]["quality"] == 0.8
    assert sizing["multipliers"]["live_credit"] == 1.2
    assert sizing["caps"]["max_notional"] == 100
    assert "抢跑试探" in "；".join(sizing["reasons"])
