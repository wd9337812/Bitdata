from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.learning_report import build_daily_learning_report, save_daily_learning_report
from app.position_sizing import effective_order_viability, effective_position_risk, signal_strength_tier, unified_position_sizing
from app.product_completion import product_completion_summary
from app.runtime_protection import build_runtime_protection_action
from app.stage_modes import stage_profile_for_equity
from app.stage_simulation import simulate_stage_path


def test_stage_profile_maps_small_equity_to_opportunity_v3_stage():
    profile = stage_profile_for_equity(50, {"stage_s0_risk_pct": 10})

    assert profile["stage"] == "S0"
    assert profile["recommended_mode"] == "extreme_sprint"
    assert profile["strategy_family"] == "extreme_v3_roll"
    assert profile["base_risk_pct"] == 10


def test_unified_position_sizing_exposes_all_multipliers():
    sizing = unified_position_sizing(
        stage_base_risk=10,
        candidate={
            "risk_adjustment": {"multiplier": 0.5},
            "quality_risk_multiplier": 0.8,
            "live_credit_adjustment": {"risk_multiplier": 1.5, "reasons": ["credit"]},
            "market_state": {"risk_multiplier": 0.7, "label": "thin"},
        },
        guard={"risk_multiplier": 0.75, "reason": "drawdown"},
        target={"effective_risk_multiplier": 1.2, "status": "behind"},
        liquidity={"risk_multiplier": 0.9},
        risk_caps={"max_risk_pct": 3},
    )

    assert sizing["final_risk_pct"] <= 3
    assert set(sizing["multipliers"]) == {
        "signal",
        "quality",
        "live_credit",
        "target_progress",
        "equity_guard",
        "market_state",
        "liquidity",
    }
    assert sizing["reasons"]


def test_effective_risk_keeps_qualified_probe_meaningful_during_drawdown():
    sizing = effective_position_risk(
        candidate_risk_pct=0.18,
        candidate={"entry_type": "extreme_probe", "score": 99, "symbol_quality": {"score": 70}},
        guard={"risk_multiplier": 0.2},
        target={"effective_risk_multiplier": 1.0},
        config={
            "effective_position_sizing_enabled": True,
            "effective_guard_probe_min_multiplier": 0.2,
            "effective_probe_min_risk_pct": 0.5,
            "effective_probe_max_risk_pct": 2.0,
        },
        mode="extreme_sprint",
    )

    assert sizing["tier"] == "probe"
    assert sizing["final_risk_pct"] == 0.5


def test_effective_risk_gives_top_signal_more_drawdown_access():
    candidate = {"entry_type": "standard", "score": 140, "symbol_quality": {"score": 80}}
    assert signal_strength_tier(candidate) == "top"
    sizing = effective_position_risk(
        candidate_risk_pct=10,
        candidate=candidate,
        guard={"risk_multiplier": 0.2},
        target={"effective_risk_multiplier": 1.0},
        config={
            "effective_position_sizing_enabled": True,
            "effective_guard_top_min_multiplier": 0.6,
            "effective_top_min_risk_pct": 2,
            "effective_top_max_risk_pct": 14,
        },
        mode="extreme_sprint",
    )

    assert sizing["guard_multiplier"] == 0.6
    assert sizing["final_risk_pct"] == 6


def test_stage_route_hard_cap_cannot_be_lifted_by_yolo_risk_floor():
    sizing = effective_position_risk(
        candidate_risk_pct=0.35,
        candidate={
            "mode": "yolo_scalp",
            "entry_type": "orderbook_impact",
            "score": 130,
            "cost_ratio": 8,
            "symbol_quality": {"score": 90},
        },
        guard={"risk_multiplier": 1.0},
        target={"effective_risk_multiplier": 1.0},
        config={
            "_stage_route": {"stage": "S3", "risk_pct": 0.35},
            "yolo_scalp_orderbook_impact_min_risk_pct": 45,
            "yolo_scalp_orderbook_impact_max_risk_pct": 85,
        },
        mode="yolo_scalp",
    )

    assert sizing["yolo_scalp_profile"]["min_risk_pct"] == 45
    assert sizing["stage_risk_cap_pct"] == 0.35
    assert sizing["final_risk_pct"] == 0.35


def test_yolo_firecracker_gets_scalp_risk_floor():
    sizing = effective_position_risk(
        candidate_risk_pct=2.2,
        candidate={
            "mode": "yolo_scalp",
            "entry_type": "extreme_probe",
            "score": 96,
            "cost_ratio": 8,
            "symbol_quality": {"score": 62},
            "live_credit": {"score": 50, "losses": 0},
        },
        guard={"risk_multiplier": 1.0},
        target={"effective_risk_multiplier": 1.0},
        config={
            "effective_position_sizing_enabled": True,
            "yolo_scalp_firecracker_min_risk_pct": 30,
            "yolo_scalp_firecracker_max_risk_pct": 70,
            "yolo_scalp_high_score": 95,
            "yolo_scalp_high_risk_multiplier": 1.35,
            "yolo_scalp_min_cost_ratio": 4,
        },
        mode="yolo_scalp",
    )

    assert sizing["scalp_tier"] == "firecracker"
    assert sizing["final_risk_pct"] == 30
    assert sizing["yolo_scalp_profile"]["label"] == "火药桶剥头皮"


def test_effective_order_viability_rejects_fee_noise_orders():
    result = effective_order_viability(
        notional=8,
        candidate={"expected_profit_pct": 1.0, "estimated_cost_pct": 0.2, "cost_ratio": 5},
        config={
            "effective_min_order_notional_usdt": 10,
            "effective_min_profit_cost_ratio": 3,
            "effective_min_net_profit_usdt": 0.15,
        },
    )

    assert result["allowed"] is False
    assert "below_effective_min_notional" in result["reasons"]
    assert "insufficient_expected_net_profit" in result["reasons"]


def test_effective_order_viability_accepts_economic_order():
    result = effective_order_viability(
        notional=30,
        candidate={"expected_profit_pct": 1.2, "estimated_cost_pct": 0.15, "cost_ratio": 8},
        config={
            "effective_min_order_notional_usdt": 10,
            "effective_min_profit_cost_ratio": 3,
            "effective_min_net_profit_usdt": 0.15,
        },
    )

    assert result["allowed"] is True
    assert result["expected_net_profit"] > 0.15


def test_runtime_protection_detects_fast_invalid_without_trade_execution():
    state = {
        "runtime_protection_positions": {
            "TESTUSDT:LONG": {"opened_at": datetime.now(timezone.utc).isoformat(), "max_hold_bars": 12}
        }
    }
    action = build_runtime_protection_action(
        {"symbol": "TESTUSDT", "positionAmt": "1", "entryPrice": "100", "markPrice": "98"},
        config={"protection_fast_invalid_seconds": 90, "protection_fast_invalid_atr": 0.35},
        state=state,
        client=None,
    )

    assert action["direction"] == "LONG"
    assert action["action"] in {"observe", "close_fast_invalid"}


def test_runtime_protection_closes_v473_stagnation_after_cost():
    state = {
        "runtime_protection_positions": {
            "TESTUSDT:LONG": {
                "opened_at": (datetime.now(timezone.utc) - timedelta(seconds=240)).isoformat(),
                "max_hold_seconds": 480,
                "stagnation_seconds": 180,
                "stagnation_min_profit_pct": 0.12,
            }
        }
    }
    action = build_runtime_protection_action(
        {"symbol": "TESTUSDT", "positionAmt": "1", "entryPrice": "100", "markPrice": "100.05"},
        config={"protection_fast_invalid_seconds": 90},
        state=state,
        client=None,
    )

    assert action["action"] == "close_stagnation"
    assert action["reason"] == "stagnation_after_cost"


@pytest.mark.parametrize(
    "protection_version",
    [
        "market_tsmom_daily_v2",
        "market_tsmom_daily_v3",
        "adaptive_30d_daily_v1",
    ],
)
def test_runtime_protection_leaves_daily_trend_position_to_daily_manager(protection_version):
    state = {
        "runtime_protection_positions": {
            "BTCUSDT:LONG": {
                "opened_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
                "max_hold_seconds": 480 * 3600,
                "protection_version": protection_version,
                "runtime_intraday_trailing_enabled": False,
            }
        }
    }
    action = build_runtime_protection_action(
        {"symbol": "BTCUSDT", "positionAmt": "0.01", "entryPrice": "100", "markPrice": "120"},
        config={"protection_fast_invalid_seconds": 90},
        state=state,
        client=None,
    )

    assert action["action"] == "observe"
    assert action["reason"] == "daily_strategy_managed"


def test_stage_simulation_returns_path_metrics():
    result = simulate_stage_path(
        {"growth_mode": "extreme_sprint", "extreme_sprint_risk_per_trade_pct": 28},
        start_equity=50,
        target_equity=10_000,
        days=30,
        trades_per_day=3,
    )

    assert result["trades"] == 90
    assert "final_equity" in result
    assert result["warning"]


def test_daily_learning_report_saves_markdown(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))

    report = save_daily_learning_report(datetime(2026, 7, 6, tzinfo=timezone.utc))
    built = build_daily_learning_report(datetime(2026, 7, 6, tzinfo=timezone.utc))

    assert report["path"].endswith("daily-learning-2026-07-06.md")
    assert "markdown" in built


def test_product_completion_reports_all_nine_modules():
    summary = product_completion_summary({})

    assert summary["complete"] is True
    assert summary["completed"] == 9
    assert len(summary["modules"]) == 9
