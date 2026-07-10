from __future__ import annotations

from app.runner import stage4_scalp_overlay_config


def test_stage4_overlay_uses_separate_low_risk_and_excludes_grid_symbols():
    config = {
        "stage_s4_scalp_overlay_enabled": True,
        "stage_s4_scalp_risk_pct": 0.1,
        "stage_s4_scalp_margin_pct": 5,
        "stage_s4_scalp_daily_loss_limit_pct": 1,
        "stage2_symbols": ["BTCUSDT", "ETHUSDT"],
    }
    state = {
        "stage_route": {
            "stage": "S4",
            "mode": "grid",
            "max_open_positions": 8,
            "leverage": 2,
        }
    }

    overlay = stage4_scalp_overlay_config(config, state)

    assert overlay is not None
    assert overlay["_active_growth_mode"] == "yolo_scalp"
    assert overlay["_stage_route"]["strategy_family"] == "orderbook_scalp"
    assert overlay["_stage_route"]["risk_pct"] == 0.1
    assert overlay["_excluded_scan_symbols"] == ["BTCUSDT", "ETHUSDT"]


def test_stage4_overlay_is_not_active_in_rolling_stages():
    assert stage4_scalp_overlay_config(
        {"stage_s4_scalp_overlay_enabled": True},
        {"stage_route": {"stage": "S0", "mode": "extreme_sprint"}},
    ) is None
