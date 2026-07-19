from app.position_sizing import effective_order_viability, effective_position_risk


def test_effective_order_viability_explains_blockers_in_chinese():
    result = effective_order_viability(
        notional=3.0,
        candidate={"expected_profit_pct": 0.12, "estimated_cost_pct": 0.08, "mode": "yolo_scalp"},
        config={
            "yolo_scalp_effective_min_order_notional_usdt": 5.0,
            "yolo_scalp_min_order_lift_min_cost_ratio": 3.0,
            "yolo_scalp_min_order_lift_min_net_profit_usdt": 0.03,
        },
    )

    assert result["allowed"] is False
    assert "below_effective_min_notional" in result["reasons"]
    assert "低于系统有效下单额" in result["reason_labels"]
    assert "当前名义金额" in result["summary"]


def test_recovery_uses_minimum_risk_cap_instead_of_multiplier_stack():
    result = effective_position_risk(
        candidate_risk_pct=10,
        candidate={
            "global_performance_guard": {"status": "recovery_2", "risk_multiplier": 0.4},
            "score": 80,
        },
        guard={"allowed": True, "risk_multiplier": 0.2},
        target={"effective_risk_multiplier": 1.0},
        config={"effective_position_sizing_enabled": True, "effective_standard_min_risk_pct": 0},
        mode="extreme_sprint",
    )

    assert result["performance_mode"] == "minimum_cap"
    assert result["performance_cap_pct"] == 4.0
    assert result["final_risk_pct"] == 2.0


def test_v432_continuous_quality_replaces_legacy_lane_cap_without_hard_tiers():
    result = effective_position_risk(
        candidate_risk_pct=4.0,
        candidate={
            "opportunity_v4": {
                "position_confidence": {
                    "applied": True,
                    "confidence": 1.0,
                    "target_initial_risk_pct": 7.5,
                    "display_label": "强机会",
                }
            },
            "global_performance_guard": {"status": "normal", "risk_multiplier": 1.0},
        },
        guard={"allowed": True, "risk_multiplier": 1.0},
        target={"effective_risk_multiplier": 1.0},
        config={
            "effective_position_sizing_enabled": True,
            "opportunity_v432_initial_max_risk_pct": 7.5,
            "_stage_route": {"risk_pct": 10.0},
        },
        mode="extreme_sprint",
    )

    assert result["continuous_quality_applied"] is True
    assert result["final_risk_pct"] == 7.5
    assert result["risk_chain"]["channel_risk_pct"] == 4.0
    assert result["risk_chain"]["continuous_quality_risk_pct"] == 7.5


def test_v432_canary_caps_continuous_risk_once_and_release_fallback_restores_base():
    candidate = {
        "opportunity_v4": {
            "position_confidence": {
                "applied": True,
                "confidence": 1.0,
                "target_initial_risk_pct": 7.5,
            }
        },
        "global_performance_guard": {"status": "strategy_canary_1", "risk_multiplier": 0.7},
    }
    normal = effective_position_risk(
        candidate_risk_pct=4.0,
        candidate=candidate,
        guard={"risk_multiplier": 1.0},
        target={"effective_risk_multiplier": 1.0},
        config={"effective_position_sizing_enabled": True, "opportunity_v432_initial_max_risk_pct": 7.5},
        mode="extreme_sprint",
    )
    fallback = effective_position_risk(
        candidate_risk_pct=4.0,
        candidate=candidate,
        guard={"risk_multiplier": 1.0},
        target={"effective_risk_multiplier": 1.0},
        config={"effective_position_sizing_enabled": True, "_release_fallback_active": True},
        mode="extreme_sprint",
    )

    assert normal["final_risk_pct"] == 5.25
    assert fallback["continuous_quality_applied"] is False
    assert fallback["release_fallback_active"] is True
    assert fallback["final_risk_pct"] == 2.8
