from __future__ import annotations

from datetime import datetime, timezone

from app.learning_report import build_daily_learning_report, save_daily_learning_report
from app.position_sizing import effective_order_viability, effective_position_risk, signal_strength_tier, unified_position_sizing
from app.product_completion import product_completion_summary
from app.runtime_protection import build_runtime_protection_action
from app.stage_modes import stage_profile_for_equity
from app.stage_simulation import simulate_stage_path


def test_stage_profile_maps_small_equity_to_yolo_stage():
    profile = stage_profile_for_equity(50, {"yolo_scalp_risk_per_trade_pct": 55})

    assert profile["stage"] == "S0"
    assert profile["recommended_mode"] == "yolo_scalp"
    assert profile["base_risk_pct"] == 55


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
