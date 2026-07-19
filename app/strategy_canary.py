from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import wraps
import threading
from typing import Any

from app.local_circuit import cohort_key
from app.state_store import load_state, save_state
from app.telemetry import record_event


CANARY_STATE_KEY = "strategy_canary"
_CANARY_LOCK = threading.RLock()


def _synchronized(function: Any) -> Any:
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with _CANARY_LOCK:
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


def _base_state(release_id: str) -> dict[str, Any]:
    return {
        "status": "inactive",
        "release_id": release_id,
        "permit_id": None,
        "permit_kind": None,
        "issued_at": None,
        "expires_at": None,
        "level": 0,
        "risk_multiplier": 0.0,
        "used_opportunities": 0,
        "max_opportunities": 0,
        "live_trades_at_issue": 0,
        "last_live_closed_total": 0,
        "wins": 0,
        "losses": 0,
        "consecutive_losses": 0,
        "probe_open": False,
        "probe_symbol": None,
        "probe_direction": None,
        "probe_cohort_key": None,
        "revoked_at": None,
        "reissue_shadow_baseline_id": 0,
        "reissue_count": 0,
        "recovery": {},
        "last_transition_at": None,
        "reason": "not_required",
    }


def _save_if_changed(previous: dict[str, Any], current: dict[str, Any], now: datetime) -> dict[str, Any]:
    old = {key: value for key, value in previous.items() if key != "last_transition_at"}
    new = {key: value for key, value in current.items() if key != "last_transition_at"}
    if old == new:
        return previous
    current = {**current, "last_transition_at": now.isoformat()}
    save_state({CANARY_STATE_KEY: current})
    if previous.get("status") != current.get("status") or previous.get("level") != current.get("level"):
        record_event(
            "info",
            "strategy_canary",
            f"新策略试运行许可证：{previous.get('status') or '未初始化'} -> {current.get('status')}",
            {
                "release_id": current.get("release_id"),
                "level": current.get("level"),
                "risk_multiplier": current.get("risk_multiplier"),
                "reason": current.get("reason"),
            },
        )
    return current


def _target_release(config: dict[str, Any], active_release_id: str) -> bool:
    configured = str(config.get("strategy_canary_release_id") or "").strip()
    return bool(configured and configured == active_release_id)


def _shadow_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(row.get("net_pnl") or 0) for row in rows]
    positive = sum(value for value in values if value > 0)
    negative = sum(value for value in values if value < 0)
    return {
        "trades": len(values),
        "symbols": len({str(row.get("symbol") or "").upper() for row in rows if row.get("symbol")}),
        "net_pnl": round(sum(values), 8),
        "profit_factor": round(positive / abs(negative), 4) if negative < 0 else (999.0 if positive > 0 else 0.0),
        "latest_id": max((int(row.get("id") or 0) for row in rows), default=0),
    }


def _reissue_evidence(
    state: dict[str, Any],
    rows: list[dict[str, Any]],
    config: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    baseline_id = int(state.get("reissue_shadow_baseline_id") or 0)
    revoked_at = _parse_time(state.get("revoked_at"))
    fresh = [
        row
        for row in rows
        if int(row.get("id") or 0) > baseline_id
        and (not revoked_at or (_parse_time(row.get("closed_at")) or now) > revoked_at)
    ]
    stats = _shadow_stats(fresh)
    observation_minutes = float(config.get("strategy_canary_reissue_observation_minutes", 60))
    observation_complete = bool(
        revoked_at and (now - revoked_at).total_seconds() >= observation_minutes * 60
    )
    ready = bool(
        config.get("strategy_canary_reissue_enabled", True)
        and observation_complete
        and stats["trades"] >= int(config.get("strategy_canary_reissue_min_shadow_trades", 8))
        and stats["symbols"] >= int(config.get("strategy_canary_reissue_min_symbols", 3))
        and stats["net_pnl"] > 0
        and stats["profit_factor"] >= float(config.get("strategy_canary_reissue_min_profit_factor", 1.15))
    )
    return {
        **stats,
        "ready": ready,
        "observation_complete": observation_complete,
        "observation_minutes": observation_minutes,
        "seconds_until_observation_complete": max(
            0,
            int(observation_minutes * 60 - (now - revoked_at).total_seconds()),
        )
        if revoked_at
        else int(observation_minutes * 60),
        "required_trades": int(config.get("strategy_canary_reissue_min_shadow_trades", 8)),
        "required_symbols": int(config.get("strategy_canary_reissue_min_symbols", 3)),
        "required_profit_factor": float(config.get("strategy_canary_reissue_min_profit_factor", 1.15)),
    }


def _issued_state(
    config: dict[str, Any],
    *,
    active_release_id: str,
    closed_total: int,
    now: datetime,
    reissued: bool,
    previous: dict[str, Any],
    startup_cap: bool = False,
    active_probe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    duration = float(config.get("strategy_canary_permit_hours", 24))
    base = _base_state(active_release_id)
    active_probe = active_probe or {}
    probe_open = bool(active_probe)
    return {
        **base,
        "status": "probe_open" if probe_open else "waiting_candidate",
        "permit_id": f"{active_release_id}:{int(now.timestamp())}",
        "permit_kind": "release_startup" if startup_cap else "risk_off_recovery",
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=duration)).isoformat(),
        "level": 1,
        "risk_multiplier": float(
            config.get("strategy_canary_reissue_multiplier", 0.30)
            if reissued
            else config.get("strategy_canary_level_1_multiplier", 0.40)
        ),
        "max_opportunities": int(
            config.get("strategy_canary_reissue_max_opportunities", 2)
            if reissued
            else config.get("strategy_canary_level_1_max_opportunities", 5)
        ),
        "live_trades_at_issue": closed_total,
        "last_live_closed_total": closed_total,
        "used_opportunities": 1 if probe_open else 0,
        "probe_open": probe_open,
        "probe_symbol": active_probe.get("symbol"),
        "probe_direction": active_probe.get("direction"),
        "reissue_count": int(previous.get("reissue_count") or 0) + (1 if reissued else 0),
        "reason": (
            "protected_canary_position_open"
            if probe_open
            else "shadow_evidence_reissued_canary"
            if reissued
            else "new_strategy_release_canary"
        ),
    }


def _active_release_probe(state: dict[str, Any], active_release_id: str) -> dict[str, Any] | None:
    _, _, active_version = active_release_id.partition("@")
    tracked = state.get("runtime_protection_positions") or {}
    if not isinstance(tracked, dict):
        return None
    for key, item in tracked.items():
        if not isinstance(item, dict) or str(item.get("strategy_version") or "") != active_version:
            continue
        symbol, _, direction = str(key).partition(":")
        return {"symbol": symbol.upper() or None, "direction": direction.upper() or None}
    return None


def _result(
    state: dict[str, Any],
    *,
    enabled: bool,
    allowed: bool,
    now: datetime,
    reason: str | None = None,
) -> dict[str, Any]:
    startup_cap = state.get("permit_kind") == "release_startup"
    expires_at = _parse_time(state.get("expires_at"))
    startup_window_active = bool(startup_cap and expires_at and now < expires_at)
    return {
        **state,
        "enabled": enabled,
        "allowed": allowed,
        "startup_cap": startup_cap,
        "startup_window_active": startup_window_active,
        "blocks_new_entries": bool(startup_window_active and not allowed),
        "reason": reason if reason is not None else state.get("reason"),
    }


@_synchronized
def strategy_canary_status(
    config: dict[str, Any],
    *,
    active_release_id: str,
    risk_off: bool,
    cooldown_active: bool,
    emergency_stop: bool,
    current_live: dict[str, Any],
    eligible_shadow_rows: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a version-scoped live canary permit without weakening account safeguards."""
    now = now or datetime.now(timezone.utc)
    enabled = bool(config.get("strategy_canary_enabled", True)) and _target_release(config, active_release_id)
    persisted_state = load_state()
    stored = persisted_state.get(CANARY_STATE_KEY)
    previous = dict(stored) if isinstance(stored, dict) else {}
    state = dict(previous) if previous.get("release_id") == active_release_id else _base_state(active_release_id)
    if not enabled:
        return _result(state, enabled=False, allowed=False, now=now, reason="release_not_authorized")

    startup_cap_enabled = bool(config.get("strategy_canary_startup_cap_enabled", False))
    if not risk_off and not startup_cap_enabled:
        normal = {**_base_state(active_release_id), "status": "not_required", "reason": "global_guard_normal"}
        state = _save_if_changed(previous, normal, now)
        return _result(state, enabled=True, allowed=False, now=now)

    closed_total = int(current_live.get("closed_total") or current_live.get("trades") or 0)
    eligible_shadow_rows = eligible_shadow_rows or []
    if not state.get("permit_id") and config.get("strategy_canary_auto_issue", True):
        state = _issued_state(
            config,
            active_release_id=active_release_id,
            closed_total=closed_total,
            now=now,
            reissued=False,
            previous=state,
            startup_cap=startup_cap_enabled,
            active_probe=_active_release_probe(persisted_state, active_release_id) if startup_cap_enabled else None,
        )
    elif startup_cap_enabled and state.get("permit_id") and not state.get("permit_kind"):
        # Migrate a permit issued by a pre-v0.13.0 process without resetting its
        # issue time, loss counter, or opportunity budget.
        state["permit_kind"] = "release_startup"

    if emergency_stop:
        revoked = {
            **state,
            "status": "revoked",
            "risk_multiplier": 0.0,
            "probe_open": False,
            "reason": "emergency_safety_stop",
        }
        state = _save_if_changed(previous, revoked, now)
        return _result(state, enabled=True, allowed=False, now=now)

    expires_at = _parse_time(state.get("expires_at"))
    if expires_at is None or now >= expires_at:
        expired = {
            **state,
            "status": "expired",
            "risk_multiplier": 0.0,
            "probe_open": False,
            "reason": "canary_permit_expired",
        }
        state = _save_if_changed(previous, expired, now)
        return _result(state, enabled=True, allowed=False, now=now)

    if state.get("status") == "revoked":
        if state.get("permit_kind") == "release_startup":
            return _result(state, enabled=True, allowed=False, now=now)
        if state.get("reason") != "canary_loss_budget_exhausted":
            return _result(state, enabled=True, allowed=False, now=now)
        recovery = _reissue_evidence(state, eligible_shadow_rows, config, now)
        state = {**state, "recovery": recovery}
        if recovery["ready"] and not cooldown_active:
            state = _issued_state(
                config,
                active_release_id=active_release_id,
                closed_total=closed_total,
                now=now,
                reissued=True,
                previous=state,
            )
        else:
            state = _save_if_changed(previous, state, now)
            return _result(
                state,
                enabled=True,
                allowed=False,
                now=now,
                reason="mandatory_cooldown" if cooldown_active else "waiting_for_reissue_shadow_evidence",
            )

    last_closed = int(state.get("last_live_closed_total") or 0)
    if closed_total > last_closed:
        newly_closed = max(1, closed_total - last_closed)
        recent_values = [float(value or 0) for value in (current_live.get("recent_net_pnls") or [])[:newly_closed]]
        if not recent_values:
            recent_values = [float(current_live.get("latest_net_pnl") or 0)] * newly_closed
        new_wins = sum(value > 0 for value in recent_values)
        new_losses = len(recent_values) - new_wins
        state["wins"] = int(state.get("wins") or 0) + new_wins
        state["losses"] = int(state.get("losses") or 0) + new_losses
        if new_losses == 0:
            state["consecutive_losses"] = 0
        else:
            trailing_losses = 0
            for value in recent_values:
                if value > 0:
                    break
                trailing_losses += 1
            state["consecutive_losses"] = trailing_losses + (
                int(state.get("consecutive_losses") or 0)
                if trailing_losses == len(recent_values)
                else 0
            )
        state["last_live_closed_total"] = closed_total
        state["probe_open"] = False
        state["probe_symbol"] = None
        state["probe_direction"] = None
        state["probe_cohort_key"] = None
        state["status"] = "waiting_candidate"

    max_losses = int(config.get("strategy_canary_max_losses", 2))
    if int(state.get("losses") or 0) >= max_losses:
        revoked = {
            **state,
            "status": "revoked",
            "risk_multiplier": 0.0,
            "probe_open": False,
            "revoked_at": now.isoformat(),
            "reissue_shadow_baseline_id": max(
                (int(row.get("id") or 0) for row in eligible_shadow_rows),
                default=0,
            ),
            "recovery": {},
            "reason": "canary_loss_budget_exhausted",
        }
        state = _save_if_changed(previous, revoked, now)
        return _result(state, enabled=True, allowed=False, now=now)

    live_net = float(current_live.get("net_pnl") or 0)
    live_pf = float(current_live.get("profit_factor") or 0)
    live_since_issue = max(0, closed_total - int(state.get("live_trades_at_issue") or 0))
    startup_cap = state.get("permit_kind") == "release_startup"
    if not startup_cap and (
        live_since_issue >= int(config.get("strategy_canary_level_3_min_trades", 8))
        and live_net > 0
        and live_pf >= float(config.get("strategy_canary_level_3_min_profit_factor", 1.15))
    ):
        state.update(
            {
                "status": "validated",
                "level": 3,
                "risk_multiplier": float(config.get("strategy_canary_level_3_multiplier", 1.0)),
                "max_opportunities": int(config.get("strategy_canary_level_3_max_opportunities", 12)),
                "reason": "canary_live_evidence_validated",
            }
        )
    elif not startup_cap and (
        live_since_issue >= int(config.get("strategy_canary_level_2_min_trades", 3))
        and live_net > 0
        and live_pf >= float(config.get("strategy_canary_level_2_min_profit_factor", 1.05))
    ):
        state.update(
            {
                "status": "level_2",
                "level": 2,
                "risk_multiplier": float(config.get("strategy_canary_level_2_multiplier", 0.70)),
                "max_opportunities": int(config.get("strategy_canary_level_2_max_opportunities", 8)),
                "reason": "canary_live_evidence_positive",
            }
        )
    elif state.get("status") not in {"waiting_candidate", "probe_open"}:
        state.update({"status": "waiting_candidate", "level": 1, "reason": "waiting_for_canary_candidate"})

    used = int(state.get("used_opportunities") or 0)
    maximum = int(state.get("max_opportunities") or 0)
    if used >= maximum and not state.get("probe_open"):
        exhausted = {**state, "status": "exhausted", "risk_multiplier": 0.0, "reason": "canary_opportunity_budget_exhausted"}
        state = _save_if_changed(previous, exhausted, now)
        return _result(state, enabled=True, allowed=False, now=now)

    state = _save_if_changed(previous, state, now)
    allowed = not cooldown_active and not bool(state.get("probe_open")) and state.get("status") in {
        "waiting_candidate",
        "level_2",
        "validated",
    }
    reason = "mandatory_cooldown" if cooldown_active else state.get("reason")
    return _result(state, enabled=True, allowed=allowed, now=now, reason=reason)


def candidate_can_use_canary(candidate: dict[str, Any] | None, permit: dict[str, Any]) -> tuple[bool, str]:
    candidate = candidate or {}
    opportunity = candidate.get("opportunity_v4") or {}
    release_id = f"{candidate.get('strategy_family')}@{candidate.get('strategy_version')}"
    if release_id != str(permit.get("release_id") or ""):
        return False, "candidate_release_mismatch"
    if permit.get("permit_kind") == "release_startup":
        if not (opportunity.get("admitted") or opportunity.get("passed")):
            return False, "candidate_not_admitted_by_release"
        return True, "startup_canary_candidate_allowed"
    if not opportunity.get("canary_eligible"):
        return False, "candidate_not_canary_eligible"
    return True, "canary_candidate_allowed"


@_synchronized
def consume_strategy_canary(
    decision: dict[str, Any],
    result: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    if result.get("mode") not in {"live", "rotation_live"}:
        return None
    if not result.get("stop_order") or not result.get("take_profit_order"):
        return None
    now = now or datetime.now(timezone.utc)
    stored = load_state().get(CANARY_STATE_KEY)
    if not isinstance(stored, dict) or stored.get("status") not in {"waiting_candidate", "level_2", "validated"}:
        return None
    candidate = decision.get("candidate") or decision
    eligible, _ = candidate_can_use_canary(candidate, stored)
    if not eligible:
        return None
    current = {
        **stored,
        "status": "probe_open",
        "used_opportunities": int(stored.get("used_opportunities") or 0) + 1,
        "probe_open": True,
        "probe_symbol": str(decision.get("symbol") or "").upper(),
        "probe_direction": str(decision.get("direction") or "").upper(),
        "probe_cohort_key": cohort_key(candidate),
        "reason": "protected_canary_position_open",
    }
    return _save_if_changed(stored, current, now)


@_synchronized
def revoke_strategy_canary(reason: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    now = now or datetime.now(timezone.utc)
    stored = load_state().get(CANARY_STATE_KEY)
    if not isinstance(stored, dict):
        return None
    current = {
        **stored,
        "status": "revoked",
        "risk_multiplier": 0.0,
        "probe_open": False,
        "revoked_at": now.isoformat(),
        "reason": reason,
    }
    return _save_if_changed(stored, current, now)
