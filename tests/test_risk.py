from app.risk import assess_new_position, live_trading_allowed, position_size_from_risk
from datetime import datetime, timedelta, timezone


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


def test_risk_ignores_legacy_symbol_cooldown_by_default():
    state = {
        "bot_status": "running",
        "symbol_cooldowns": {
            "SOLUSDT": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        },
    }
    decision = assess_new_position(base_config(), state, 50, "SOLUSDT", [])
    assert decision.allowed is True


def test_risk_blocks_legacy_symbol_cooldown_when_enabled():
    config = base_config()
    config["legacy_symbol_cooldown_blocks"] = True
    state = {
        "bot_status": "running",
        "symbol_cooldowns": {
            "SOLUSDT": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        },
    }
    decision = assess_new_position(config, state, 50, "SOLUSDT", [])
    assert decision.allowed is False
    assert decision.reason == "symbol_cooldown_active"


def test_risk_blocks_only_matching_direction_cooldown():
    state = {
        "bot_status": "running",
        "symbol_direction_cooldowns": {
            "SOLUSDT:LONG": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        },
    }

    long_decision = assess_new_position(base_config(), state, 50, "SOLUSDT", [], overrides={"direction": "LONG"})
    short_decision = assess_new_position(base_config(), state, 50, "SOLUSDT", [], overrides={"direction": "SHORT"})

    assert long_decision.allowed is False
    assert long_decision.reason == "symbol_direction_cooldown_active"
    assert short_decision.allowed is True


def test_risk_blocks_max_drawdown_by_default():
    state = {
        "bot_status": "running",
        "equity_high_watermark": 100,
        "daily_start_equity": 72,
    }
    decision = assess_new_position(base_config(), state, 72, "SOLUSDT", [])
    assert decision.allowed is False
    assert decision.reason == "max_drawdown_limit"


def test_tournament_can_ignore_high_watermark_drawdown():
    state = {
        "bot_status": "running",
        "equity_high_watermark": 100,
        "daily_start_equity": 72,
    }
    decision = assess_new_position(
        base_config(),
        state,
        72,
        "SOLUSDT",
        [],
        overrides={"ignore_max_drawdown": True, "daily_loss_limit_pct": 25, "margin_pct": 90, "leverage": 5},
    )
    assert decision.allowed is True
    assert decision.reason == "allowed"


def test_rotation_can_ignore_max_open_positions_explicitly():
    state = {"bot_status": "running", "daily_start_equity": 50, "equity_high_watermark": 50}
    open_positions = [{"symbol": "OLDUSDT", "positionAmt": "1"}]

    blocked = assess_new_position(base_config(), state, 50, "NEWUSDT", open_positions)
    allowed = assess_new_position(
        base_config(),
        state,
        50,
        "NEWUSDT",
        open_positions,
        overrides={"ignore_max_open_positions": True},
    )

    assert blocked.reason == "max_open_positions"
    assert allowed.allowed is True


def test_risk_allows_sprint_consecutive_loss_override():
    config = base_config()
    state = {"bot_status": "running", "daily_start_equity": 50, "equity_high_watermark": 50, "consecutive_losses": 3}

    blocked = assess_new_position(config, state, 50, "SOLUSDT", [])
    allowed = assess_new_position(
        config,
        state,
        50,
        "SOLUSDT",
        [],
        overrides={"max_consecutive_losses": 4, "margin_pct": 95, "leverage": 5},
    )

    assert blocked.reason == "consecutive_loss_limit"
    assert allowed.allowed is True
