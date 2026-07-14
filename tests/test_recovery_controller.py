from datetime import datetime, timedelta, timezone

from app.recovery_controller import consume_recovery_permit, recovery_permit_status
from app.state_store import daily_session_state_updates, load_state, save_state


def _config() -> dict:
    return {
        "performance_recovery_permit_enabled": True,
        "performance_guard_recovery_shadow_trades": 20,
        "performance_recovery_entry_profit_factor": 0.9,
        "performance_recovery_confirm_closes": 3,
        "performance_recovery_confirm_minutes": 5,
        "performance_recovery_permit_minutes": 180,
        "performance_recovery_revoke_profit_factor": 0.5,
        "performance_recovery_revoke_new_shadow_trades": 3,
        "performance_guard_recovery_level_2_multiplier": 0.4,
    }


def _shadow(pf: float = 1.1, net: float = 0.5) -> dict:
    return {"trades": 20, "wins": 8, "net_pnl": net, "profit_factor": pf}


def test_daily_session_resets_once_per_utc_day():
    first = datetime(2026, 7, 15, 0, 1, tzinfo=timezone.utc)
    state = {"daily_session_date": "2026-07-14", "daily_start_equity": 30, "daily_realized_pnl": -4}

    updates = daily_session_state_updates(state, 19.5, now=first)
    unchanged = daily_session_state_updates({**state, **updates}, 21, now=first + timedelta(hours=2))

    assert updates["daily_session_date"] == "2026-07-15"
    assert updates["daily_start_equity"] == 19.5
    assert updates["daily_realized_pnl"] == 0.0
    assert unchanged == {}


def test_recovery_permit_latches_until_candidate_arrives(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    now = datetime(2026, 7, 15, 1, 0, tzinfo=timezone.utc)
    common = {
        "strategy_version": "v3.2",
        "risk_off": True,
        "cooldown_active": False,
        "emergency_stop": False,
        "shadow_tail": _shadow(),
        "shadow_closed_total": 100,
        "current_live": {"trades": 0, "latest_net_pnl": 0},
    }

    one = recovery_permit_status(_config(), **common, shadow_token="100:a", now=now)
    two = recovery_permit_status(_config(), **{**common, "shadow_closed_total": 101}, shadow_token="101:b", now=now + timedelta(minutes=1))
    permit = recovery_permit_status(_config(), **{**common, "shadow_closed_total": 102}, shadow_token="102:c", now=now + timedelta(minutes=2))
    still_waiting = recovery_permit_status(
        _config(),
        **{**common, "shadow_tail": _shadow(pf=0.8, net=-0.01), "shadow_closed_total": 103},
        shadow_token="103:d",
        now=now + timedelta(minutes=30),
    )

    assert one["status"] == "confirming"
    assert two["allowed"] is False
    assert permit["status"] == "waiting_candidate"
    assert permit["allowed"] is True
    assert still_waiting["allowed"] is True
    assert still_waiting["permit_id"] == permit["permit_id"]


def test_hard_shadow_failure_revokes_latched_permit(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    now = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)
    save_state(
        {
            "performance_recovery": {
                "status": "waiting_candidate",
                "strategy_version": "v3.2",
                "permit_id": "v3.2:1",
                "permit_issued_at": now.isoformat(),
                "permit_expires_at": (now + timedelta(hours=3)).isoformat(),
                "shadow_trades_at_issue": 100,
            }
        }
    )

    result = recovery_permit_status(
        _config(),
        strategy_version="v3.2",
        risk_off=True,
        cooldown_active=False,
        emergency_stop=False,
        shadow_tail=_shadow(pf=0.2, net=-1),
        shadow_token="103:z",
        shadow_closed_total=103,
        current_live={"trades": 0, "latest_net_pnl": 0},
        now=now + timedelta(minutes=10),
    )

    assert result["allowed"] is False
    assert result["status"] == "accumulating"
    assert result["reason"] == "shadow_evidence_hard_failure"


def test_expired_permit_requires_fresh_shadow_evidence_before_reissue(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    now = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)
    save_state(
        {
            "performance_recovery": {
                "status": "waiting_candidate",
                "strategy_version": "v3.2",
                "permit_id": "v3.2:expired",
                "permit_issued_at": (now - timedelta(hours=4)).isoformat(),
                "permit_expires_at": (now - timedelta(hours=1)).isoformat(),
                "shadow_trades_at_issue": 100,
                "qualified_closes": 3,
                "qualified_since": (now - timedelta(hours=4)).isoformat(),
                "last_shadow_token": "100:a",
            }
        }
    )

    expired = recovery_permit_status(
        _config(),
        strategy_version="v3.2",
        risk_off=True,
        cooldown_active=False,
        emergency_stop=False,
        shadow_tail=_shadow(),
        shadow_token="100:a",
        shadow_closed_total=100,
        current_live={"trades": 0, "latest_net_pnl": 0},
        now=now,
    )
    fresh = recovery_permit_status(
        _config(),
        strategy_version="v3.2",
        risk_off=True,
        cooldown_active=False,
        emergency_stop=False,
        shadow_tail=_shadow(),
        shadow_token="101:b",
        shadow_closed_total=101,
        current_live={"trades": 0, "latest_net_pnl": 0},
        now=now + timedelta(minutes=1),
    )

    assert expired["allowed"] is False
    assert expired["qualified_closes"] == 0
    assert fresh["status"] == "confirming"
    assert fresh["qualified_closes"] == 1


def test_permit_is_consumed_only_after_protected_live_result(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_state(
        {
            "performance_recovery": {
                "status": "waiting_candidate",
                "strategy_version": "v3.2",
                "permit_id": "v3.2:2",
                "permit_expires_at": "2026-07-15T06:00:00+00:00",
            }
        }
    )
    decision = {
        "symbol": "SOLUSDT",
        "direction": "LONG",
        "performance_guard": {"current_live": {"trades": 1}},
    }

    assert consume_recovery_permit(decision, {"mode": "blocked"}) is None
    consumed = consume_recovery_permit(
        decision,
        {"mode": "live", "stop_order": {"id": 1}, "take_profit_order": {"id": 2}},
        now=datetime(2026, 7, 15, 3, 0, tzinfo=timezone.utc),
    )

    assert consumed is not None
    assert consumed["status"] == "probe_open"
    assert load_state()["performance_recovery"]["probe_symbol"] == "SOLUSDT"
