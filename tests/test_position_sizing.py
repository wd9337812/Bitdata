from app.position_sizing import effective_order_viability


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
