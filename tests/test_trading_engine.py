from app.state_store import load_state, save_state
from app.trading_engine import sync_stage


def test_sync_stage_initializes_extreme_sprint_equity_guard(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    config = {
        "growth_mode": "extreme_sprint",
        "extreme_sprint_enabled": True,
        "extreme_sprint_confirmation": "ENABLE_EXTREME_SPRINT",
        "auto_risk_by_equity": False,
        "stage1_target_equity": 10000,
        "stage2_activation": "manual",
        "extreme_sprint_interval": "5m",
        "extreme_sprint_recent_days": 2,
        "extreme_sprint_risk_per_trade_pct": 28,
        "extreme_sprint_max_leverage": 8,
        "extreme_sprint_max_symbol_margin_pct": 98,
    }
    state = {
        **load_state(),
        "equity_high_watermark": 100,
        "equity_guard_mode": "balanced",
        "extreme_sprint_equity_high_watermark": 0,
    }
    save_state(state)

    updated = sync_stage(config, state, {"equity": 60})

    assert updated["equity_high_watermark"] == 100
    assert updated["equity_guard_mode"] == "extreme_sprint"
    assert updated["extreme_sprint_start_equity"] == 60
    assert updated["extreme_sprint_equity_high_watermark"] == 60


def test_sync_stage_keeps_extreme_sprint_high_watermark(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    config = {
        "growth_mode": "extreme_sprint",
        "extreme_sprint_enabled": True,
        "extreme_sprint_confirmation": "ENABLE_EXTREME_SPRINT",
        "auto_risk_by_equity": False,
        "stage1_target_equity": 10000,
        "stage2_activation": "manual",
        "extreme_sprint_interval": "5m",
        "extreme_sprint_recent_days": 2,
        "extreme_sprint_risk_per_trade_pct": 28,
        "extreme_sprint_max_leverage": 8,
        "extreme_sprint_max_symbol_margin_pct": 98,
    }
    state = {
        **load_state(),
        "equity_guard_mode": "extreme_sprint",
        "extreme_sprint_start_equity": 60,
        "extreme_sprint_equity_high_watermark": 66,
    }
    save_state(state)

    updated = sync_stage(config, state, {"equity": 62})

    assert updated["extreme_sprint_start_equity"] == 60
    assert updated["extreme_sprint_equity_high_watermark"] == 66
