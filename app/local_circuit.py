from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps
import threading
from typing import Any

from app.market_structure import market_structure, normalize_setup_type
from app.state_store import load_state, save_state
from app.telemetry import record_event


LOCAL_CIRCUIT_STATE_KEY = "v4_local_circuit"
LIVE_ELIGIBLE_LANES = {
    "core_canary",
    "core_provisional",
    "validated",
    "limited_exploration",
}
_LOCAL_CIRCUIT_LOCK = threading.RLock()


def _synchronized(function: Any) -> Any:
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with _LOCAL_CIRCUIT_LOCK:
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


def _cohort_parts(candidate: dict[str, Any]) -> dict[str, str]:
    structure = market_structure(candidate)
    signal = candidate.get("signal") or {}
    return {
        "market_regime": str(
            structure.get("market_regime")
            or (candidate.get("market_state") or {}).get("state")
            or "unknown"
        ).lower(),
        "direction": str(candidate.get("direction") or signal.get("signal") or "").upper(),
        "setup_type": normalize_setup_type(
            structure.get("setup_type") or candidate.get("entry_type") or signal.get("entry_type") or "unknown"
        ),
        "entry_phase": str(
            structure.get("entry_phase")
            or signal.get("entry_phase")
            or candidate.get("entry_phase")
            or "UNKNOWN"
        ).upper(),
    }


def cohort_key(candidate: dict[str, Any]) -> str:
    parts = _cohort_parts(candidate)
    return ":".join(
        (
            parts["market_regime"],
            parts["direction"],
            parts["setup_type"],
            parts["entry_phase"],
        )
    )


def evidence_cohort_key(row: dict[str, Any]) -> str:
    return ":".join(
        (
            str(row.get("market_regime") or "unknown").lower(),
            str(row.get("direction") or "").upper(),
            normalize_setup_type(row.get("entry_type") or "unknown"),
            str(row.get("entry_phase") or "UNKNOWN").upper(),
        )
    )


def _base_state(release_id: str) -> dict[str, Any]:
    return {
        "release_id": release_id,
        "open_positions": {},
        "processed_closes": [],
        "cohorts": {},
        "updated_at": None,
    }


def local_circuit_state(release_id: str) -> dict[str, Any]:
    stored = load_state().get(LOCAL_CIRCUIT_STATE_KEY)
    if not isinstance(stored, dict) or stored.get("release_id") != release_id:
        return _base_state(release_id)
    return stored


@_synchronized
def record_v4_live_open(
    decision: dict[str, Any],
    result: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    if result.get("mode") not in {"live", "rotation_live"}:
        return None
    if not result.get("stop_order") or not result.get("take_profit_order"):
        return None
    candidate = decision.get("candidate") or decision
    family = str(candidate.get("strategy_family") or decision.get("strategy_family") or "")
    version = str(candidate.get("strategy_version") or decision.get("strategy_version") or "")
    if family != "extreme_v4_roll" or not version:
        return None
    symbol = str(decision.get("symbol") or candidate.get("symbol") or "").upper()
    direction = str(decision.get("direction") or candidate.get("direction") or "").upper()
    if not symbol or direction not in {"LONG", "SHORT"}:
        return None
    now = now or datetime.now(timezone.utc)
    release_id = f"{family}@{version}"
    state = local_circuit_state(release_id)
    positions = dict(state.get("open_positions") or {})
    key = cohort_key(candidate)
    positions[f"{symbol}:{direction}"] = {
        "symbol": symbol,
        "direction": direction,
        "cohort_key": key,
        "parts": _cohort_parts(candidate),
        "opened_at": now.isoformat(),
    }
    updated = {**state, "open_positions": positions, "updated_at": now.isoformat()}
    save_state({LOCAL_CIRCUIT_STATE_KEY: updated})
    return updated


@_synchronized
def reconcile_v4_local_circuit(
    config: dict[str, Any],
    *,
    release_id: str,
    live_rows: list[dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    state = local_circuit_state(release_id)
    positions = dict(state.get("open_positions") or {})
    cohorts = dict(state.get("cohorts") or {})
    processed = list(state.get("processed_closes") or [])
    processed_set = set(processed)
    changed = state.get("release_id") != release_id
    live_loss_limit = int(config.get("opportunity_v431_local_live_loss_streak", 2))

    for row in sorted(live_rows, key=lambda item: int(item.get("close_time") or 0)):
        symbol = str(row.get("symbol") or "").upper()
        direction = str(row.get("direction") or "").upper()
        close_time = int(row.get("close_time") or 0)
        token = f"{symbol}:{direction}:{close_time}"
        if not symbol or not close_time or token in processed_set:
            continue
        position_key = f"{symbol}:{direction}"
        probe = positions.pop(position_key, None)
        key = str((probe or {}).get("cohort_key") or row.get("cohort_key") or "")
        processed.append(token)
        processed_set.add(token)
        changed = True
        if not key:
            continue
        cohort = dict(cohorts.get(key) or {})
        net_pnl = float(row.get("net_pnl") or 0)
        previous_streak = int(cohort.get("live_loss_streak") or 0)
        live_loss_streak = previous_streak + 1 if net_pnl < 0 else 0
        cohort.update(
            {
                "cohort_key": key,
                "parts": (probe or {}).get("parts") or cohort.get("parts") or {},
                "live_loss_streak": live_loss_streak,
                "last_live_net_pnl": round(net_pnl, 8),
                "last_live_close_time": close_time,
                "last_live_close_at": datetime.fromtimestamp(close_time / 1000, timezone.utc).isoformat(),
            }
        )
        if live_loss_streak >= live_loss_limit:
            was_blocked = bool(cohort.get("blocked_at"))
            cohort.update(
                {
                    "blocked_at": cohort["last_live_close_at"],
                    "block_reason": "two_consecutive_live_losses",
                }
            )
            if not was_blocked:
                record_event(
                    "warning",
                    "v4_local_circuit",
                    "V4 局部组合连续净亏损，已转为影子观察",
                    {"release_id": release_id, "cohort_key": key, "live_loss_streak": live_loss_streak},
                )
        cohorts[key] = cohort

    if not changed:
        return state
    updated = {
        **state,
        "release_id": release_id,
        "open_positions": positions,
        "processed_closes": processed[-200:],
        "cohorts": cohorts,
        "updated_at": now.isoformat(),
    }
    save_state({LOCAL_CIRCUIT_STATE_KEY: updated})
    return updated


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(row.get("net_pct") or 0) for row in rows]
    positive = sum(value for value in values if value > 0)
    negative = sum(value for value in values if value < 0)
    return {
        "trades": len(values),
        "symbols": len({str(row.get("symbol") or "").upper() for row in rows if row.get("symbol")}),
        "net_pct": round(sum(values), 6),
        "profit_factor": round(positive / abs(negative), 4) if negative < 0 else (999.0 if positive > 0 else 0.0),
    }


def _shadow_circuit(
    rows: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    min_trades = int(config.get("opportunity_v431_local_block_min_trades", 8))
    block_pf = float(config.get("opportunity_v431_local_block_profit_factor", 0.80))
    restore_trades = int(config.get("opportunity_v431_local_restore_min_trades", 8))
    restore_symbols = int(config.get("opportunity_v431_local_restore_min_symbols", 3))
    restore_pf = float(config.get("opportunity_v431_local_restore_profit_factor", 1.05))
    segment: list[dict[str, Any]] = []
    recovery: list[dict[str, Any]] = []
    blocked = False
    blocked_at: str | None = None
    restored_at: str | None = None
    trigger_stats = _stats([])

    for row in sorted(rows, key=lambda item: str(item.get("closed_at") or "")):
        if blocked:
            if str(row.get("admission_lane") or "") in LIVE_ELIGIBLE_LANES:
                recovery.append(row)
            recovery_stats = _stats(recovery)
            if (
                recovery_stats["trades"] >= restore_trades
                and recovery_stats["symbols"] >= restore_symbols
                and recovery_stats["net_pct"] > 0
                and recovery_stats["profit_factor"] >= restore_pf
            ):
                blocked = False
                restored_at = str(row.get("closed_at") or "")
                segment = []
                recovery = []
            continue
        segment.append(row)
        current = _stats(segment)
        if current["trades"] >= min_trades and current["net_pct"] < 0 and current["profit_factor"] < block_pf:
            blocked = True
            blocked_at = str(row.get("closed_at") or "")
            trigger_stats = current
            recovery = []

    return {
        "blocked": blocked,
        "blocked_at": blocked_at if blocked else None,
        "restored_at": restored_at,
        "trigger": trigger_stats,
        "recovery": _stats(recovery),
        "requirements": {
            "block_trades": min_trades,
            "block_profit_factor": block_pf,
            "restore_trades": restore_trades,
            "restore_symbols": restore_symbols,
            "restore_profit_factor": restore_pf,
        },
    }


def candidate_local_circuit_status(
    candidate: dict[str, Any],
    evidence_rows: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    version = str(config.get("opportunity_v4_strategy_version") or "v4.3.2")
    release_id = f"extreme_v4_roll@{version}"
    state = state or local_circuit_state(release_id)
    key = cohort_key(candidate)
    parts = _cohort_parts(candidate)
    local_rows = [row for row in evidence_rows if evidence_cohort_key(row) == key]
    shadow = _shadow_circuit(local_rows, config)
    stored = dict((state.get("cohorts") or {}).get(key) or {})
    live_blocked_at = _parse_time(stored.get("blocked_at"))
    live_recovery_rows = [
        row
        for row in local_rows
        if live_blocked_at
        and (_parse_time(row.get("closed_at")) or datetime.min.replace(tzinfo=timezone.utc)) > live_blocked_at
        and str(row.get("admission_lane") or "") in LIVE_ELIGIBLE_LANES
    ]
    live_recovery = _stats(live_recovery_rows)
    restore_trades = int(config.get("opportunity_v431_local_restore_min_trades", 8))
    restore_symbols = int(config.get("opportunity_v431_local_restore_min_symbols", 3))
    restore_pf = float(config.get("opportunity_v431_local_restore_profit_factor", 1.05))
    live_restored = bool(
        live_blocked_at
        and live_recovery["trades"] >= restore_trades
        and live_recovery["symbols"] >= restore_symbols
        and live_recovery["net_pct"] > 0
        and live_recovery["profit_factor"] >= restore_pf
    )
    live_blocked = bool(live_blocked_at and not live_restored)
    blocked = bool(shadow["blocked"] or live_blocked)
    reason = (
        "two_consecutive_live_losses"
        if live_blocked
        else "local_shadow_negative"
        if shadow["blocked"]
        else "local_shadow_recovered"
        if live_restored or shadow.get("restored_at")
        else "local_circuit_clear"
    )
    return {
        "enabled": bool(config.get("opportunity_v431_local_circuit_enabled", True)),
        "release_id": release_id,
        "cohort_key": key,
        "parts": parts,
        "blocked": blocked if config.get("opportunity_v431_local_circuit_enabled", True) else False,
        "reason": reason,
        "live_loss_streak": int(stored.get("live_loss_streak") or 0),
        "live_blocked_at": stored.get("blocked_at") if live_blocked else None,
        "live_recovery": live_recovery,
        "shadow": shadow,
        "restored": bool(live_restored or shadow.get("restored_at")),
    }
