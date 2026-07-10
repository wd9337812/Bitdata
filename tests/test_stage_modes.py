from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.scanner import mode_config
from app.stage_modes import all_stage_profiles, apply_stage_route, resolve_stage_route, stage_profile_for_equity


def test_equity_profiles_follow_confirmed_automatic_route():
    assert stage_profile_for_equity(50)["stage"] == "S0"
    assert stage_profile_for_equity(50)["risk_pct"] == 10
    assert stage_profile_for_equity(300)["stage"] == "S1"
    assert stage_profile_for_equity(10_000)["stage"] == "S2"
    assert stage_profile_for_equity(100_000)["stage"] == "S3"
    assert stage_profile_for_equity(1_000_000)["stage"] == "S4"
    assert all_stage_profiles()[-1]["max_equity"] is None
    json.dumps(all_stage_profiles(), allow_nan=False)


def test_upward_switch_requires_buffer_and_three_confirmations():
    config = {"stage_routing_enabled": True, "stage_switch_up_buffer_pct": 5, "stage_switch_confirmations": 3}
    state = {"active_stage": "S2", "active_growth_mode": "extreme_sprint"}

    held = resolve_stage_route(config, state, 104_999)
    assert held["stage"] == "S2"
    assert held["reason"] == "hysteresis_hold"

    first = resolve_stage_route(config, state, 105_001)
    assert first["stage"] == "S2"
    assert first["desired_stage"] == "S3"
    assert first["confirmation_count"] == 1
    state.update({"stage_route_candidate": "S3", "stage_route_confirmation_count": 1})
    second = resolve_stage_route(config, state, 105_001)
    assert second["stage"] == "S2"
    state["stage_route_confirmation_count"] = 2
    third = resolve_stage_route(config, state, 105_001)
    assert third["stage"] == "S3"
    assert third["mode"] == "yolo_scalp"


def test_downward_switch_uses_ten_percent_hysteresis():
    config = {"stage_routing_enabled": True, "stage_switch_down_buffer_pct": 10, "stage_switch_confirmations": 1}
    state = {"active_stage": "S3", "active_growth_mode": "yolo_scalp"}

    assert resolve_stage_route(config, state, 90_001)["stage"] == "S3"
    route = resolve_stage_route(config, state, 89_999)
    assert route["stage"] == "S2"
    assert route["mode"] == "extreme_sprint"


def test_mode_change_waits_until_position_is_flat():
    config = {"stage_routing_enabled": True, "stage_switch_up_buffer_pct": 5, "stage_switch_confirmations": 1}
    state = {"active_stage": "S2", "active_growth_mode": "extreme_sprint"}

    pending = resolve_stage_route(config, state, 110_000, has_open_positions=True)
    assert pending["stage"] == "S2"
    assert pending["pending"] is True
    assert pending["pending_mode"] == "yolo_scalp"

    switched = resolve_stage_route(config, state, 110_000, has_open_positions=False)
    assert switched["stage"] == "S3"
    assert switched["pending"] is False


def test_manual_override_keeps_equity_stage_and_expires():
    now = datetime.now(timezone.utc)
    config = {
        "stage_routing_enabled": True,
        "stage_manual_mode": "yolo_scalp",
        "stage_manual_until": (now + timedelta(hours=1)).isoformat(),
    }
    manual = resolve_stage_route(config, {}, 50, now=now)
    assert manual["stage"] == "S0"
    assert manual["mode"] == "yolo_scalp"
    assert manual["source"] == "manual"
    assert manual["risk_pct"] == 10

    expired = resolve_stage_route(config, {}, 50, now=now + timedelta(hours=2))
    assert expired["mode"] == "extreme_sprint"
    assert expired["source"] == "auto"


def test_route_overlay_reaches_actual_scanner_mode_parameters():
    route = resolve_stage_route({"stage_routing_enabled": True}, {}, 50)
    runtime = apply_stage_route({"growth_mode": "balanced", "extreme_sprint_interval": "5m"}, route)
    mode = mode_config(runtime, 50)

    assert mode["mode"] == "extreme_sprint"
    assert mode["stage"] == "S0"
    assert mode["risk_pct"] == 10
    assert mode["max_open_positions"] == 1
