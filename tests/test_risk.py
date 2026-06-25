from app.risk import assess_new_position, live_trading_allowed, position_size_from_risk


def base_config():
    return {
        "dry_run": True,
        "live_trading_enabled": False,
        "live_trading_confirmation": "",
        "max_consecutive_losses": 2,
        "daily_loss_limit_pct": 3,
        "max_drawdown_pct": 15,
        "max_open_positions": 1,
        "max_symbol_margin_pct": 35,
        "stage1_max_leverage": 2,
    }


def test_live_trading_requires_all_switches():
    config = base_config()
    assert live_trading_allowed(config) is False
    config.update({
        "dry_run": False,
        "live_trading_enabled": True,
        "live_trading_confirmation": "ENABLE_LIVE_TRADING",
    })
    assert live_trading_allowed(config) is True


def test_risk_blocks_paused_bot():
    decision = assess_new_position(base_config(), {"bot_status": "paused"}, 50, "SOLUSDT", [])
    assert decision.allowed is False
    assert decision.reason == "bot_paused"


def test_position_size_from_fixed_risk():
    assert position_size_from_risk(100, 1, 10, 9) == 1
