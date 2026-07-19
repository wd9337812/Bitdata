from datetime import datetime, timedelta, timezone

from app.strategy_canary import (
    candidate_can_use_canary,
    consume_strategy_canary,
    strategy_canary_status,
)
from app.state_store import save_state


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


def _startup_config() -> dict:
    return {
        **_config(),
        "strategy_canary_release_id": "extreme_v4_roll@v4.3.2",
        "strategy_canary_startup_cap_enabled": True,
        "strategy_canary_level_1_multiplier": 0.70,
        "strategy_canary_level_1_max_opportunities": 5,
    }


def test_startup_canary_caps_normal_guard_and_adopts_existing_protected_position(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    now = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    save_state(
        {
            "runtime_protection_positions": {
                "BUSDT:LONG": {"strategy_version": "v4.3.2", "opened_at": (now - timedelta(minutes=5)).isoformat()}
            }
        }
    )

    permit = strategy_canary_status(
        _startup_config(),
        active_release_id="extreme_v4_roll@v4.3.2",
        risk_off=False,
        cooldown_active=False,
        emergency_stop=False,
        current_live=_live(),
        now=now,
    )
    candidate = {
        "strategy_family": "extreme_v4_roll",
        "strategy_version": "v4.3.2",
        "opportunity_v4": {"admitted": True, "canary_eligible": False},
    }
    candidate_allowed, candidate_reason = candidate_can_use_canary(candidate, permit)

    assert permit["permit_kind"] == "release_startup"
    assert permit["startup_window_active"] is True
    assert permit["status"] == "probe_open"
    assert permit["used_opportunities"] == 1
    assert permit["allowed"] is False
    assert permit["blocks_new_entries"] is True
    assert candidate_allowed is True
    assert candidate_reason == "startup_canary_candidate_allowed"


def test_startup_canary_resets_after_close_revokes_at_two_losses_and_releases_after_expiry(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    config = _startup_config()
    now = datetime(2026, 7, 19, 11, 0, tzinfo=timezone.utc)
    candidate = {
        "symbol": "ALTUSDT",
        "strategy_family": "extreme_v4_roll",
        "strategy_version": "v4.3.2",
        "opportunity_v4": {"admitted": True},
    }
    protected = {"mode": "live", "stop_order": {"id": 1}, "take_profit_order": {"id": 2}}

    permit = strategy_canary_status(
        config,
        active_release_id="extreme_v4_roll@v4.3.2",
        risk_off=False,
        cooldown_active=False,
        emergency_stop=False,
        current_live=_live(),
        now=now,
    )
    assert permit["allowed"] is True
    for index in range(2):
        consume_strategy_canary(
            {"symbol": "ALTUSDT", "direction": "LONG", "candidate": candidate},
            protected,
            now=now + timedelta(minutes=index * 5 + 1),
        )
        permit = strategy_canary_status(
            config,
            active_release_id="extreme_v4_roll@v4.3.2",
            risk_off=False,
            cooldown_active=False,
            emergency_stop=False,
            current_live={**_live(closed=index + 1, net=-0.2 * (index + 1), pf=0, latest=-0.2), "recent_net_pnls": [-0.2]},
            now=now + timedelta(minutes=index * 5 + 2),
        )
        if index == 0:
            assert permit["status"] == "waiting_candidate"
            assert permit["allowed"] is True

    expired = strategy_canary_status(
        config,
        active_release_id="extreme_v4_roll@v4.3.2",
        risk_off=False,
        cooldown_active=False,
        emergency_stop=False,
        current_live=_live(closed=2, net=-0.4),
        now=now + timedelta(hours=25),
    )

    assert permit["status"] == "revoked"
    assert permit["losses"] == 2
    assert permit["blocks_new_entries"] is True
    assert expired["status"] == "expired"
    assert expired["startup_window_active"] is False
    assert expired["blocks_new_entries"] is False


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


def test_canary_loss_revocation_reissues_after_observation_and_fresh_shadow_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    config = {
        **_config(),
        "strategy_canary_reissue_observation_minutes": 60,
        "strategy_canary_reissue_min_shadow_trades": 8,
        "strategy_canary_reissue_min_symbols": 3,
        "strategy_canary_reissue_min_profit_factor": 1.15,
        "strategy_canary_reissue_multiplier": 0.3,
        "strategy_canary_reissue_max_opportunities": 2,
    }
    now = datetime(2026, 7, 17, 3, 0, tzinfo=timezone.utc)
    candidate = {
        "symbol": "ALTUSDT",
        "direction": "LONG",
        "strategy_family": "extreme_v4_roll",
        "strategy_version": "v4.1",
        "signal": {"signal": "LONG", "entry_phase": "RETEST"},
        "market_state": {"state": "quiet"},
        "entry_type": "pullback",
        "opportunity_v4": {"canary_eligible": True},
    }
    protected = {"mode": "live", "stop_order": {"id": 1}, "take_profit_order": {"id": 2}}

    strategy_canary_status(
        config,
        active_release_id="extreme_v4_roll@v4.1",
        risk_off=True,
        cooldown_active=False,
        emergency_stop=False,
        current_live=_live(),
        now=now,
    )
    for index in range(2):
        consume_strategy_canary(
            {"symbol": "ALTUSDT", "direction": "LONG", "candidate": candidate},
            protected,
            now=now + timedelta(minutes=index * 5 + 1),
        )
        revoked = strategy_canary_status(
            config,
            active_release_id="extreme_v4_roll@v4.1",
            risk_off=True,
            cooldown_active=False,
            emergency_stop=False,
            current_live=_live(closed=index + 1, net=-0.2 * (index + 1), pf=0, latest=-0.2),
            eligible_shadow_rows=[{"id": 10, "closed_at": now.isoformat(), "symbol": "BASEUSDT", "net_pnl": 0.1}],
            now=now + timedelta(minutes=index * 5 + 2),
        )

    fresh = [
        {
            "id": 11 + index,
            "closed_at": (now + timedelta(minutes=70 + index)).isoformat(),
            "symbol": f"WIN{index % 3}USDT",
            "net_pnl": 0.2,
        }
        for index in range(8)
    ]
    reissued = strategy_canary_status(
        config,
        active_release_id="extreme_v4_roll@v4.1",
        risk_off=True,
        cooldown_active=False,
        emergency_stop=False,
        current_live=_live(closed=2, net=-0.4, pf=0, latest=-0.2),
        eligible_shadow_rows=fresh,
        now=now + timedelta(minutes=80),
    )

    assert revoked["status"] == "revoked"
    assert reissued["status"] == "waiting_candidate"
    assert reissued["allowed"] is True
    assert reissued["risk_multiplier"] == 0.3
    assert reissued["max_opportunities"] == 2
    assert reissued["reissue_count"] == 1
