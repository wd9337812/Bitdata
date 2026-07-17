from datetime import datetime, timedelta, timezone

from app.strategy_canary import (
    candidate_can_use_canary,
    consume_strategy_canary,
    strategy_canary_status,
)


def _config() -> dict:
    return {
        "strategy_canary_enabled": True,
        "strategy_canary_auto_issue": True,
        "strategy_canary_release_id": "extreme_v4_roll@v4.1",
        "strategy_canary_permit_hours": 24,
        "strategy_canary_level_1_multiplier": 0.4,
        "strategy_canary_level_1_max_opportunities": 3,
        "strategy_canary_level_2_min_trades": 3,
        "strategy_canary_level_2_min_profit_factor": 1.05,
        "strategy_canary_level_2_multiplier": 0.7,
        "strategy_canary_level_2_max_opportunities": 8,
        "strategy_canary_level_3_min_trades": 8,
        "strategy_canary_level_3_min_profit_factor": 1.15,
        "strategy_canary_level_3_multiplier": 1.0,
        "strategy_canary_level_3_max_opportunities": 12,
        "strategy_canary_max_losses": 2,
    }


def _live(closed: int = 0, net: float = 0.0, pf: float = 0.0, latest: float = 0.0) -> dict:
    return {"closed_total": closed, "trades": closed, "net_pnl": net, "profit_factor": pf, "latest_net_pnl": latest}


def test_canary_is_exact_release_scoped_and_keeps_hard_stop(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    now = datetime(2026, 7, 17, 1, 0, tzinfo=timezone.utc)

    wrong = strategy_canary_status(
        _config(),
        active_release_id="extreme_v4_roll@v4.0",
        risk_off=True,
        cooldown_active=False,
        emergency_stop=False,
        current_live=_live(),
        now=now,
    )
    stopped = strategy_canary_status(
        _config(),
        active_release_id="extreme_v4_roll@v4.1",
        risk_off=True,
        cooldown_active=False,
        emergency_stop=True,
        current_live=_live(),
        now=now,
    )

    assert wrong["allowed"] is False
    assert wrong["reason"] == "release_not_authorized"
    assert stopped["allowed"] is False
    assert stopped["status"] == "revoked"


def test_canary_waits_for_eligible_candidate_and_promotes_on_live_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    now = datetime(2026, 7, 17, 2, 0, tzinfo=timezone.utc)
    permit = strategy_canary_status(
        _config(),
        active_release_id="extreme_v4_roll@v4.1",
        risk_off=True,
        cooldown_active=False,
        emergency_stop=False,
        current_live=_live(),
        now=now,
    )
    candidate = {
        "strategy_family": "extreme_v4_roll",
        "strategy_version": "v4.1",
        "opportunity_v4": {"canary_eligible": True},
    }
    allowed, _ = candidate_can_use_canary(candidate, permit)
    consumed = consume_strategy_canary(
        {"symbol": "ALTUSDT", "direction": "SHORT", "candidate": candidate},
        {"mode": "live", "stop_order": {"id": 1}, "take_profit_order": {"id": 2}},
        now=now + timedelta(minutes=1),
    )
    promoted = strategy_canary_status(
        _config(),
        active_release_id="extreme_v4_roll@v4.1",
        risk_off=True,
        cooldown_active=False,
        emergency_stop=False,
        current_live=_live(closed=3, net=0.5, pf=1.2, latest=0.2),
        now=now + timedelta(hours=1),
    )

    assert permit["allowed"] is True
    assert permit["risk_multiplier"] == 0.4
    assert allowed is True
    assert consumed and consumed["status"] == "probe_open"
    assert promoted["level"] == 2
    assert promoted["risk_multiplier"] == 0.7
