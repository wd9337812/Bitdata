from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import wraps
import threading
from typing import Any

from app.state_store import load_state, save_state
from app.telemetry import record_event


RECOVERY_STATE_KEY = "performance_recovery"
_RECOVERY_LOCK = threading.RLock()


def _synchronized(function: Any) -> Any:
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with _RECOVERY_LOCK:
            return function(*args, **kwargs)

    return wrapped


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _base_state(version: str) -> dict[str, Any]:
    return {
        "status": "accumulating",
        "strategy_version": version,
        "qualified_closes": 0,
        "qualified_since": None,
        "last_shadow_token": None,
        "permit_id": None,
        "permit_issued_at": None,
        "permit_expires_at": None,
        "shadow_trades_at_issue": 0,
        "consumed_at": None,
        "probe_symbol": None,
        "probe_direction": None,
        "live_trades_at_consume": 0,
        "last_transition_at": None,
        "reason": "waiting_for_current_shadow_evidence",
    }


def _save_if_changed(previous: dict[str, Any], current: dict[str, Any], now: datetime) -> dict[str, Any]:
    previous_comparable = {key: value for key, value in previous.items() if key != "last_transition_at"}
    current_comparable = {key: value for key, value in current.items() if key != "last_transition_at"}
    if previous_comparable == current_comparable:
        return previous
    current = {**current, "last_transition_at": now.isoformat()}
    save_state({RECOVERY_STATE_KEY: current})
    if previous.get("status") != current.get("status"):
        record_event(
            "info",
            "performance_recovery",
            f"实盘恢复状态：{previous.get('status') or '未初始化'} -> {current.get('status')}",
            {
                "strategy_version": current.get("strategy_version"),
                "reason": current.get("reason"),
                "permit_expires_at": current.get("permit_expires_at"),
            },
        )
    return current


@_synchronized
def recovery_permit_status(
    config: dict[str, Any],
    *,
    strategy_version: str,
    risk_off: bool,
    cooldown_active: bool,
    emergency_stop: bool,
    shadow_tail: dict[str, Any],
    shadow_token: str | None,
    shadow_closed_total: int,
    current_live: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist a recovery permit so evidence and a tradable candidate need not coincide."""
    now = now or datetime.now(timezone.utc)
    if not config.get("performance_recovery_permit_enabled", True):
        return {
            "enabled": False,
            "allowed": not risk_off and not cooldown_active,
            "status": "disabled",
            "risk_multiplier": 1.0 if not risk_off else 0.0,
        }

    stored = load_state().get(RECOVERY_STATE_KEY)
    previous = dict(stored) if isinstance(stored, dict) else {}
    state = dict(previous) if previous.get("strategy_version") == strategy_version else _base_state(strategy_version)

    if not risk_off:
        normal = {
            **_base_state(strategy_version),
            "status": "normal",
            "reason": "current_release_performance_normal",
        }
        state = _save_if_changed(previous, normal, now)
        return {**state, "enabled": True, "allowed": True, "risk_multiplier": 1.0, "recovery_level": 0}

    hard_stop = float(config.get("hard_stop_equity", config.get("tournament_stop_equity", 5.0)))
    if emergency_stop or cooldown_active:
        revoked = {
            **state,
            "status": "revoked" if emergency_stop else "cooldown",
            "permit_id": None,
            "permit_issued_at": None,
            "permit_expires_at": None,
            "consumed_at": None,
            "reason": "emergency_safety_stop" if emergency_stop else "mandatory_cooldown",
        }
        state = _save_if_changed(previous, revoked, now)
        return {**state, "enabled": True, "allowed": False, "risk_multiplier": 0.0, "recovery_level": 0, "hard_stop_equity": hard_stop}

    permit_expires = _parse_time(state.get("permit_expires_at"))
    permit_active = state.get("status") == "waiting_candidate" and permit_expires is not None and now < permit_expires
    probe_open = state.get("status") == "probe_open"
    live_count = int(current_live.get("closed_total") or current_live.get("trades") or 0)

    if state.get("status") == "waiting_candidate" and permit_expires is not None and now >= permit_expires:
        state = {
            **_base_state(strategy_version),
            "last_shadow_token": shadow_token,
            "reason": "recovery_permit_expired_waiting_for_fresh_evidence",
        }
        permit_active = False

    if probe_open and live_count > int(state.get("live_trades_at_consume") or 0):
        latest_net = float(current_live.get("latest_net_pnl") or 0)
        if latest_net > 0:
            duration = float(config.get("performance_recovery_permit_minutes", 180))
            state = {
                **state,
                "status": "waiting_candidate",
                "permit_id": f"{strategy_version}:{int(now.timestamp())}",
                "permit_issued_at": now.isoformat(),
                "permit_expires_at": (now + timedelta(minutes=duration)).isoformat(),
                "shadow_trades_at_issue": shadow_closed_total,
                "consumed_at": None,
                "probe_symbol": None,
                "probe_direction": None,
                "reason": "profitable_probe_allows_next_sample",
            }
            permit_active = True
            permit_expires = _parse_time(state.get("permit_expires_at"))
            probe_open = False
        else:
            state = {
                **_base_state(strategy_version),
                "last_shadow_token": shadow_token,
                "reason": "recovery_probe_closed_at_a_loss",
            }
            probe_open = False

    new_shadow_since_issue = max(0, shadow_closed_total - int(state.get("shadow_trades_at_issue") or 0))
    hard_shadow_failure = (
        int(shadow_tail.get("trades") or 0) >= int(config.get("performance_guard_recovery_shadow_trades", 20))
        and float(shadow_tail.get("net_pnl") or 0) < 0
        and float(shadow_tail.get("profit_factor") or 0) < float(config.get("performance_recovery_revoke_profit_factor", 0.5))
        and new_shadow_since_issue >= int(config.get("performance_recovery_revoke_new_shadow_trades", 3))
    )
    if permit_active and hard_shadow_failure:
        state = {
            **_base_state(strategy_version),
            "last_shadow_token": shadow_token,
            "reason": "shadow_evidence_hard_failure",
        }
        state = _save_if_changed(previous, state, now)
        return {**state, "enabled": True, "allowed": False, "risk_multiplier": 0.0, "recovery_level": 1}

    if permit_active:
        state = _save_if_changed(previous, state, now)
        return {
            **state,
            "enabled": True,
            "allowed": True,
            "risk_multiplier": float(config.get("performance_guard_recovery_level_2_multiplier", 0.4)),
            "recovery_level": 2,
            "seconds_remaining": max(0, int((permit_expires - now).total_seconds())) if permit_expires else 0,
        }

    if probe_open:
        state = _save_if_changed(previous, state, now)
        return {**state, "enabled": True, "allowed": False, "risk_multiplier": 0.0, "recovery_level": 2}

    entry_pf = float(config.get("performance_recovery_entry_profit_factor", 0.9))
    required_trades = int(config.get("performance_guard_recovery_shadow_trades", 20))
    qualified_now = (
        int(shadow_tail.get("trades") or 0) >= required_trades
        and float(shadow_tail.get("net_pnl") or 0) > 0
        and float(shadow_tail.get("profit_factor") or 0) >= entry_pf
    )
    if qualified_now:
        if shadow_token and shadow_token != state.get("last_shadow_token"):
            state["qualified_closes"] = int(state.get("qualified_closes") or 0) + 1
            state["last_shadow_token"] = shadow_token
        if not state.get("qualified_since"):
            state["qualified_since"] = now.isoformat()
        qualified_since = _parse_time(state.get("qualified_since")) or now
        confirmed = (
            int(state.get("qualified_closes") or 0) > 0
            and (
                int(state.get("qualified_closes") or 0) >= int(config.get("performance_recovery_confirm_closes", 3))
                or (now - qualified_since).total_seconds() >= float(config.get("performance_recovery_confirm_minutes", 5)) * 60
            )
        )
        if confirmed:
            duration = float(config.get("performance_recovery_permit_minutes", 180))
            state.update(
                {
                    "status": "waiting_candidate",
                    "permit_id": f"{strategy_version}:{int(now.timestamp())}",
                    "permit_issued_at": now.isoformat(),
                    "permit_expires_at": (now + timedelta(minutes=duration)).isoformat(),
                    "shadow_trades_at_issue": shadow_closed_total,
                    "reason": "shadow_recovery_confirmed",
                }
            )
            state = _save_if_changed(previous, state, now)
            return {
                **state,
                "enabled": True,
                "allowed": True,
                "risk_multiplier": float(config.get("performance_guard_recovery_level_2_multiplier", 0.4)),
                "recovery_level": 2,
                "seconds_remaining": int(duration * 60),
            }
        state.update({"status": "confirming", "reason": "shadow_recovery_waiting_for_stability"})
    else:
        state.update(
            {
                "status": "accumulating",
                "qualified_closes": 0,
                "qualified_since": None,
                "last_shadow_token": shadow_token,
                "permit_id": None,
                "permit_issued_at": None,
                "permit_expires_at": None,
                "reason": "waiting_for_current_shadow_evidence",
            }
        )

    state = _save_if_changed(previous, state, now)
    return {**state, "enabled": True, "allowed": False, "risk_multiplier": 0.0, "recovery_level": 1}


@_synchronized
def consume_recovery_permit(decision: dict[str, Any], result: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any] | None:
    if result.get("mode") not in {"live", "rotation_live"}:
        return None
    state = load_state()
    current = state.get(RECOVERY_STATE_KEY)
    if not isinstance(current, dict) or current.get("status") != "waiting_candidate":
        return None
    now = now or datetime.now(timezone.utc)
    guard = decision.get("performance_guard") or {}
    current_live = guard.get("current_live") or {}
    live_count = int(current_live.get("closed_total") or current_live.get("trades") or 0)
    updated = {
        **current,
        "status": "probe_open",
        "consumed_at": now.isoformat(),
        "probe_symbol": decision.get("symbol"),
        "probe_direction": decision.get("direction") or (decision.get("signal") or {}).get("signal"),
        "live_trades_at_consume": live_count,
        "permit_expires_at": None,
        "reason": "protected_live_probe_opened",
    }
    return _save_if_changed(current, updated, now)


@_synchronized
def revoke_recovery_permit(reason: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    state = load_state()
    current = state.get(RECOVERY_STATE_KEY)
    if not isinstance(current, dict) or current.get("status") not in {"waiting_candidate", "probe_open", "confirming"}:
        return None
    now = now or datetime.now(timezone.utc)
    updated = {
        **current,
        "status": "revoked",
        "permit_id": None,
        "permit_expires_at": None,
        "reason": reason,
    }
    return _save_if_changed(current, updated, now)
