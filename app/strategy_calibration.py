from __future__ import annotations

import json
import math
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.telemetry import connect, db_path


_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _parse_iso_ms(value: Any) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except ValueError:
        return None


def _decision_metadata(payload: str | dict[str, Any] | None) -> dict[str, str]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload or "{}")
        except json.JSONDecodeError:
            payload = {}
    payload = payload or {}
    decision = payload.get("decision") if isinstance(payload, dict) else {}
    if not isinstance(decision, dict):
        decision = {}
    candidate = decision.get("candidate") or (decision.get("scan") or {}).get("best") or {}
    if not isinstance(candidate, dict):
        candidate = {}
    opportunity = candidate.get("opportunity_v3") or {}
    return {
        "strategy_family": str(candidate.get("strategy_family") or decision.get("strategy_family") or "legacy_mixed"),
        "strategy_version": str(candidate.get("strategy_version") or opportunity.get("strategy_version") or "legacy"),
        "tier": str(candidate.get("v3_tier") or opportunity.get("tier") or "UNKNOWN"),
        "market_regime": str(opportunity.get("market_regime") or (candidate.get("market_state") or {}).get("state") or "unknown"),
        "entry_type": str(candidate.get("entry_type") or decision.get("entry_type") or "unknown"),
    }


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(row.get("net_pct") or 0) for row in rows]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    positive = sum(wins)
    negative = sum(losses)
    count = len(values)
    probability = (len(wins) + 2.0) / (count + 4.0) if count else 0.5
    standard_error = math.sqrt(probability * (1.0 - probability) / max(count + 4.0, 1.0))
    conservative_probability = max(0.0, probability - 1.28 * standard_error)
    average_win = positive / len(wins) if wins else 0.0
    average_loss = abs(negative) / len(losses) if losses else 0.0
    expectancy = probability * average_win - (1.0 - probability) * average_loss
    lower_expectancy = conservative_probability * average_win - (1.0 - conservative_probability) * average_loss
    return {
        "trades": count,
        "wins": len(wins),
        "win_rate": round(len(wins) / count * 100, 2) if count else 0.0,
        "profit_factor": round(positive / abs(negative), 4) if negative < 0 else (999.0 if positive > 0 else 0.0),
        "net_pct": round(sum(values), 6),
        "expected_net_pct": round(expectancy, 6),
        "lower_expected_net_pct": round(lower_expectancy, 6),
        "average_win_pct": round(average_win, 6),
        "average_loss_pct": round(average_loss, 6),
    }


def _cohort_key(meta: dict[str, str], direction: str, *, exact: bool) -> str:
    parts = [meta["strategy_family"], meta["strategy_version"], meta["tier"]]
    if exact:
        parts.extend([meta["market_regime"], direction, meta["entry_type"]])
    return "|".join(parts)


def _load_calibration(config: dict[str, Any]) -> dict[str, Any]:
    lookback_days = float(config.get("opportunity_v3_calibration_lookback_days", 30))
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    cutoff_ms = int(cutoff.timestamp() * 1000)
    live_limit = int(config.get("opportunity_v3_calibration_max_live_trades", 500))
    shadow_limit = int(config.get("opportunity_v3_calibration_max_shadow_trades", 1500))
    with connect() as conn:
        try:
            live_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT symbol, direction, open_time, open_notional, net_pnl FROM live_trade_records "
                    "WHERE close_time >= ? ORDER BY close_time DESC LIMIT ?",
                    (cutoff_ms, live_limit),
                ).fetchall()
            ]
            strategy_row_limit = int(config.get("opportunity_v3_calibration_max_strategy_runs", 2500))
            strategy_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT ts, symbol, action, payload FROM ("
                    "SELECT id, ts, symbol, action, payload FROM strategy_runs ORDER BY id DESC LIMIT ?"
                    ") WHERE ts >= ? AND action LIKE 'OPEN_%' ORDER BY id DESC",
                    (strategy_row_limit, cutoff.isoformat()),
                ).fetchall()
            ]
        except sqlite3.OperationalError:
            live_rows, strategy_rows = [], []
        try:
            shadow_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT symbol, direction, signal_type, strategy_family, market_regime, notional, net_pnl, payload "
                    "FROM shadow_trades WHERE status = 'CLOSED' AND closed_at >= ? ORDER BY id DESC LIMIT ?",
                    (cutoff.isoformat(), shadow_limit),
                ).fetchall()
            ]
        except sqlite3.OperationalError:
            shadow_rows = []

    entries: dict[tuple[str, str], list[tuple[int, dict[str, str]]]] = {}
    for row in strategy_rows:
        direction = str(row.get("action") or "").replace("OPEN_", "").upper()
        opened_ms = _parse_iso_ms(row.get("ts"))
        if direction not in {"LONG", "SHORT"} or opened_ms is None:
            continue
        entries.setdefault((str(row.get("symbol") or "").upper(), direction), []).append(
            (opened_ms, _decision_metadata(row.get("payload")))
        )

    live_groups: dict[str, list[dict[str, Any]]] = {}
    shadow_groups: dict[str, list[dict[str, Any]]] = {}
    match_window_ms = int(float(config.get("strategy_credit_match_window_minutes", 30)) * 60_000)
    for row in live_rows:
        symbol = str(row.get("symbol") or "").upper()
        direction = str(row.get("direction") or "").upper()
        opened_ms = int(row.get("open_time") or 0)
        matches = entries.get((symbol, direction), [])
        nearest = min(matches, key=lambda item: abs(item[0] - opened_ms), default=None)
        if nearest is None or abs(nearest[0] - opened_ms) > match_window_ms:
            continue
        meta = nearest[1]
        notional = max(float(row.get("open_notional") or 0), 0.00000001)
        item = {"net_pct": float(row.get("net_pnl") or 0) / notional * 100}
        for exact in (False, True):
            live_groups.setdefault(_cohort_key(meta, direction, exact=exact), []).append(item)

    for row in shadow_rows:
        try:
            payload = json.loads(row.get("payload") or "{}")
        except json.JSONDecodeError:
            payload = {}
        meta = {
            "strategy_family": str(row.get("strategy_family") or "legacy_mixed"),
            "strategy_version": str(payload.get("strategy_version") or "legacy"),
            "tier": str(payload.get("tier") or "UNKNOWN"),
            "market_regime": str(row.get("market_regime") or "unknown"),
            "entry_type": str(row.get("signal_type") or "unknown"),
        }
        direction = str(row.get("direction") or "").upper()
        notional = max(float(row.get("notional") or 0), 0.00000001)
        item = {"net_pct": float(row.get("net_pnl") or 0) / notional * 100}
        for exact in (False, True):
            shadow_groups.setdefault(_cohort_key(meta, direction, exact=exact), []).append(item)

    keys = set(live_groups) | set(shadow_groups)
    return {
        key: {"live": _stats(live_groups.get(key, [])), "shadow": _stats(shadow_groups.get(key, []))}
        for key in keys
    }


def clear_calibration_cache() -> None:
    _CACHE.clear()


def calibration_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    cache_key = str(db_path())
    now = time.monotonic()
    ttl = float(config.get("opportunity_v3_calibration_cache_seconds", 60))
    cached = _CACHE.get(cache_key)
    if cached and now - cached[0] <= ttl:
        return cached[1]
    value = _load_calibration(config)
    _CACHE[cache_key] = (now, value)
    return value


def calibrate_v3_opportunity(
    opportunity: dict[str, Any],
    signal: dict[str, Any],
    direction: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    result = dict(opportunity)
    version = str(config.get("opportunity_v3_strategy_version", "v3.2"))
    result["strategy_version"] = version
    if not config.get("opportunity_v3_calibration_enabled", True):
        result["calibration"] = {"enabled": False, "validated": False}
        return result
    meta = {
        "strategy_family": str(result.get("strategy_family") or "extreme_v3_roll"),
        "strategy_version": version,
        "tier": str(result.get("tier") or "UNKNOWN"),
        "market_regime": str(result.get("market_regime") or "unknown"),
        "entry_type": str(signal.get("entry_type") or "unknown"),
    }
    snapshots = calibration_snapshot(config)
    broad = snapshots.get(_cohort_key(meta, direction.upper(), exact=False), {"live": _stats([]), "shadow": _stats([])})
    exact = snapshots.get(_cohort_key(meta, direction.upper(), exact=True), {"live": _stats([]), "shadow": _stats([])})
    live = broad["live"]
    shadow = broad["shadow"]
    validated = bool(
        live["trades"] >= int(config.get("opportunity_v3_calibration_live_min_trades", 30))
        and shadow["trades"] >= int(config.get("opportunity_v3_calibration_shadow_min_trades", 70))
        and live["profit_factor"] >= float(config.get("opportunity_v3_calibration_min_profit_factor", 1.2))
        and shadow["profit_factor"] >= float(config.get("opportunity_v3_calibration_min_profit_factor", 1.2))
        and min(live["lower_expected_net_pct"], shadow["lower_expected_net_pct"])
        >= float(config.get("opportunity_v3_calibration_min_net_expectancy_pct", 0.02))
    )
    negative = bool(
        live["trades"] >= int(config.get("opportunity_v3_calibration_negative_live_trades", 10))
        and live["profit_factor"] < float(config.get("opportunity_v3_calibration_negative_profit_factor", 0.7))
        and live["net_pct"] < 0
    )
    if meta["tier"] == "A+" and not validated:
        result["risk_multiplier"] = min(
            float(result.get("risk_multiplier") or 0),
            float(config.get("opportunity_v3_unvalidated_a_plus_risk_multiplier", 0.65)),
        )
        result["tier_label"] = "A+ 待校准（按 A 仓位）"
        result["canary_eligible"] = False
    if negative:
        result["passed"] = False
        result["risk_multiplier"] = 0.0
        blockers = list(result.get("blockers") or [])
        blockers.append("同版本实盘样本为显著负期望")
        result["blockers"] = blockers
    result["calibration"] = {
        "enabled": True,
        "strategy_version": version,
        "validated": validated,
        "negative": negative,
        "broad": broad,
        "exact": exact,
        "label": "证据通过" if validated else "负期望停用" if negative else "样本积累中",
    }
    return result
