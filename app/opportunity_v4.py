from __future__ import annotations

import json
import math
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from statistics import mean, pstdev
from typing import Any

from app.adaptive_calibration import adaptive_calibration
from app.local_circuit import candidate_local_circuit_status, local_circuit_state
from app.market_structure import MARKET_STRUCTURE_SCHEMA, market_structure, normalize_setup_type
from app.telemetry import connect, db_path


V4_STRATEGY_FAMILY = "extreme_v4_roll"
V4_CONTROL_FAMILY = "extreme_v4_control"
V4_FEATURE_SCHEMA = "v4.9"

V462_FEATURE_WEIGHTS = {
    "cross_sectional_strength": 0.08,
    "regime_fit": 0.15,
    "volume_persistence": 0.15,
    "directed_flow": 0.20,
    "medium_path": 0.17,
    "medium_alignment": 0.08,
    "entry_quality": 0.12,
    "anti_chase": 0.04,
    "liquidity": 0.01,
}

V462_SETUP_ADJUSTMENTS = {
    "momentum": 0.04,
    "breakout": 0.01,
    "pullback": -0.03,
    "prebreakout": -0.03,
}

_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _percentile_rank(values: list[float], value: float) -> float:
    if len(values) <= 1:
        return 1.0
    below = sum(1 for item in values if item < value)
    equal = sum(1 for item in values if item == value)
    return _clamp((below + max(0, equal - 1) * 0.5) / (len(values) - 1), 0.0, 1.0)


def _entry_phase(candidate: dict[str, Any]) -> str:
    signal = candidate.get("signal") or {}
    return str(signal.get("entry_phase") or candidate.get("entry_phase") or "UNKNOWN").upper()


def _market_regime(candidate: dict[str, Any]) -> str:
    return str(market_structure(candidate).get("market_regime") or "unknown").lower()


def _load_evidence(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Load one independent decision result per opportunity for the active V4 release."""
    lookback_hours = float(config.get("opportunity_v4_evidence_lookback_hours", 168))
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat()
    limit = int(config.get("opportunity_v4_evidence_max_trades", 2500))
    with connect() as conn:
        try:
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(shadow_trades)").fetchall()}
            evidence_expr = "COALESCE(evidence_type, 'decision')" if "evidence_type" in columns else "'decision'"
            rows = conn.execute(
                f"SELECT id, symbol, closed_at, direction, signal_type, market_regime, notional, net_pnl, "
                f"estimated_cost, opportunity_id, payload, {evidence_expr} AS evidence_type FROM shadow_trades "
                "WHERE status = 'CLOSED' AND strategy_family = ? AND strategy_version = ? "
                "AND closed_at >= ? ORDER BY id DESC LIMIT ?",
                (
                    V4_STRATEGY_FAMILY,
                    str(config.get("opportunity_v4_strategy_version") or "v4.3.2"),
                    cutoff,
                    limit,
                ),
            ).fetchall()
        except sqlite3.OperationalError:
            return []

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        item = dict(row)
        if str(item.get("evidence_type") or "decision") != "decision":
            continue
        opportunity_id = str(item.get("opportunity_id") or "").strip()
        dedupe_key = opportunity_id or f"row:{item.get('id')}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        try:
            payload = json.loads(item.get("payload") or "{}")
        except (json.JSONDecodeError, TypeError):
            payload = {}
        payload_features = payload.get("features") if isinstance(payload.get("features"), dict) else {}
        notional = max(float(item.get("notional") or 0), 0.00000001)
        closed_at = str(item.get("closed_at") or "")
        result.append(
            {
                "symbol": str(item.get("symbol") or "").upper(),
                "closed_at": closed_at,
                "time_block": closed_at[:13] if closed_at else "unknown",
                "direction": str(item.get("direction") or "").upper(),
                "entry_type": normalize_setup_type(item.get("signal_type") or "unknown"),
                "market_regime": str(item.get("market_regime") or "unknown").lower(),
                "entry_phase": str(payload_features.get("entry_phase") or payload.get("entry_phase") or "UNKNOWN").upper(),
                "medium_trend_aligned": bool(payload_features.get("medium_trend_aligned", payload.get("medium_trend_aligned"))),
                "opportunity_id": opportunity_id,
                "rank_bucket": str(payload.get("rank_bucket") or "unknown"),
                "admission_lane": str(payload.get("admission_lane") or "shadow_only"),
                "cost_pct": abs(float(item.get("estimated_cost") or 0)) / notional * 100,
                "net_pct": float(item.get("net_pnl") or 0) / notional * 100,
            }
        )
    return result


def evidence_rows(config: dict[str, Any]) -> list[dict[str, Any]]:
    key = f"{db_path()}:{config.get('opportunity_v4_strategy_version', 'v4.3.2')}"
    now = time.monotonic()
    ttl = float(config.get("opportunity_v4_evidence_cache_seconds", 60))
    cached = _CACHE.get(key)
    if cached and now - cached[0] <= ttl:
        return cached[1]
    rows = _load_evidence(config)
    _CACHE[key] = (now, rows)
    return rows


def clear_v4_evidence_cache() -> None:
    _CACHE.clear()


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(row.get("net_pct") or 0) for row in rows]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    positive = sum(wins)
    negative = sum(losses)
    count = len(values)
    average = mean(values) if values else 0.0
    standard_error = pstdev(values) / math.sqrt(count) if count >= 2 else 0.50
    lower = average - 1.28 * standard_error
    return {
        "trades": count,
        "opportunities": len({row.get("opportunity_id") for row in rows if row.get("opportunity_id")}),
        "symbols": len({row.get("symbol") for row in rows if row.get("symbol")}),
        "time_blocks": len({row.get("time_block") for row in rows if row.get("time_block")}),
        "win_rate": round(len(wins) / count * 100, 2) if count else 0.0,
        "profit_factor": round(positive / abs(negative), 4) if negative < 0 else (999.0 if positive > 0 else 0.0),
        "net_pct": round(sum(values), 6),
        "expected_net_pct": round(average, 6),
        "lower_expected_net_pct": round(lower, 6),
        "standard_error_pct": round(standard_error, 6),
        "average_cost_pct": round(mean([float(row.get("cost_pct") or 0) for row in rows]), 6) if rows else 0.0,
    }


def _shrink_expectancy(
    local: dict[str, Any],
    prior_expected: float,
    prior_lower: float,
    prior_trades: float,
) -> tuple[float, float]:
    trades = float(local.get("trades") or 0)
    if trades <= 0:
        return prior_expected, prior_lower
    weight = trades / (trades + max(prior_trades, 1.0))
    expected = float(local.get("expected_net_pct") or 0) * weight + prior_expected * (1.0 - weight)
    lower = float(local.get("lower_expected_net_pct") or 0) * weight + prior_lower * (1.0 - weight)
    return expected, lower


def _cohort_evidence(candidate: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Return normalized local evidence plus a broad direction risk background."""
    direction = str(candidate.get("direction") or "").upper()
    structure = market_structure(candidate)
    entry_type = normalize_setup_type(structure.get("setup_type") or candidate.get("entry_type") or "unknown")
    regime = _market_regime(candidate)
    phase = _entry_phase(candidate)
    rows = evidence_rows(config)
    exact_phase = [
        row
        for row in rows
        if row["direction"] == direction
        and normalize_setup_type(row["entry_type"]) == entry_type
        and row["market_regime"] == regime
        and row["entry_phase"] == phase
    ]
    exact = [
        row
        for row in rows
        if row["direction"] == direction
        and normalize_setup_type(row["entry_type"]) == entry_type
        and row["market_regime"] == regime
    ]
    setup = [
        row
        for row in rows
        if row["direction"] == direction and normalize_setup_type(row["entry_type"]) == entry_type
    ]
    direction_rows = [row for row in rows if row["direction"] == direction]
    stats = {
        "exact_phase": _stats(exact_phase),
        "exact": _stats(exact),
        "setup": _stats(setup),
        "direction": _stats(direction_rows),
    }
    if exact_phase:
        selected, scope = exact_phase, "regime_direction_setup_phase"
    elif exact:
        selected, scope = exact, "regime_direction_setup"
    elif setup:
        selected, scope = setup, "direction_setup"
    else:
        selected, scope = [], "model_only"

    prior_trades = float(config.get("opportunity_v43_hierarchy_prior_trades", 30))
    direction_stats = stats["direction"]
    expected = float(direction_stats["expected_net_pct"]) if direction_stats["trades"] else 0.0
    lower = float(direction_stats["lower_expected_net_pct"]) if direction_stats["trades"] else 0.0
    for level in (stats["setup"], stats["exact"], stats["exact_phase"]):
        expected, lower = _shrink_expectancy(level, expected, lower, prior_trades)
    local_stats = _stats(selected)
    return {
        "scope": scope,
        "selected": local_stats,
        **stats,
        "hierarchical": {
            "scope": scope,
            "local_trades": int(local_stats["trades"]),
            "expected_net_pct": round(expected, 6),
            "lower_expected_net_pct": round(lower, 6),
            "prior_trades": int(prior_trades),
        },
    }


def _direction_evidence_multiplier(evidence: dict[str, Any], config: dict[str, Any]) -> float:
    """Use broad history as a position-size modifier, never as a candidate veto."""
    direction = evidence.get("direction") or {}
    trades = int(direction.get("trades") or 0)
    if trades < int(config.get("opportunity_v43_direction_risk_min_trades", 20)):
        return 1.0
    profit_factor = float(direction.get("profit_factor") or 0)
    if profit_factor < float(config.get("opportunity_v43_direction_low_pf", 0.55)):
        return float(config.get("opportunity_v43_direction_low_multiplier", 0.55))
    if profit_factor < float(config.get("opportunity_v43_direction_medium_pf", 0.75)):
        return float(config.get("opportunity_v43_direction_medium_multiplier", 0.70))
    if profit_factor < float(config.get("opportunity_v43_direction_full_pf", 1.0)):
        return float(config.get("opportunity_v43_direction_caution_multiplier", 0.85))
    return 1.0


def _model_features(candidate: dict[str, Any]) -> dict[str, float]:
    signal = candidate.get("signal") or {}
    structure = market_structure(candidate)
    depth = candidate.get("depth") or {}
    phase = _entry_phase(candidate)
    strength = float(structure.get("strength_percentile") or 0.5)
    direction_fit = _clamp(float(structure.get("direction_multiplier") or 0.8) / 1.15, 0.0, 1.0)
    volume = _clamp((float(signal.get("volume_acceleration") or 1.0) - 0.75) / 1.5, 0.0, 1.0)
    flow = _clamp((float(signal.get("directed_trade_flow") or 0.5) - 0.42) / 0.20, 0.0, 1.0)
    path = _clamp(float(structure.get("medium_path_efficiency") or 0.0) / 0.35, 0.0, 1.0)
    medium = 1.0 if structure.get("medium_trend_aligned") else 0.35 if structure.get("medium_ready") else 0.15
    entry_quality = {"RETEST": 1.0, "ARMED": 0.68, "TRIGGERED": 0.30}.get(phase, 0.45)
    extension = float(signal.get("breakout_extension_atr") or 0.0)
    impulse = max(0.0, float(signal.get("impulse_atr") or 0.0))
    wick = float(signal.get("adverse_wick_ratio") or 0.0)
    anti_chase = 1.0 - _clamp(max(extension / 1.0, impulse / 2.0, wick / 4.0), 0.0, 1.0)
    spread_raw = depth.get("spread_pct")
    spread = float(spread_raw) if spread_raw is not None else 999.0
    depth_notional = float(depth.get("depth_notional") or 0.0)
    liquidity = 0.0 if spread >= 999 else 0.55 * _clamp(1.0 - spread / 0.15, 0.0, 1.0) + 0.45 * _clamp(depth_notional / 10_000, 0.0, 1.0)
    smart_flow_alignment = _clamp(float((candidate.get("smart_flow") or {}).get("directional_alignment") or 0.0), -1.0, 1.0)
    return {
        "cross_sectional_strength": strength,
        "regime_fit": direction_fit,
        "volume_persistence": volume,
        "directed_flow": flow,
        "medium_path": path,
        "medium_alignment": medium,
        "entry_quality": entry_quality,
        "anti_chase": anti_chase,
        "liquidity": liquidity,
        "smart_flow_alignment": smart_flow_alignment,
    }


def _dynamic_cost_pct(candidate: dict[str, Any], config: dict[str, Any]) -> float:
    depth = candidate.get("depth") or {}
    fee_pct = float(config.get("taker_fee_pct_round_trip", 0.08))
    slippage_pct = float(candidate.get("estimated_slippage_pct") or config.get("estimated_slippage_pct", 0.04))
    spread_pct = max(0.0, float(depth.get("spread_pct") or 0.0))
    buffer_pct = float(config.get("opportunity_v41_cost_safety_buffer_pct", 0.02))
    measured = float(candidate.get("estimated_cost_pct") or 0.0)
    return max(measured, fee_pct + slippage_pct + spread_pct + buffer_pct)


def _v48_exhaustion(candidate: dict[str, Any], features: dict[str, float], config: dict[str, Any]) -> dict[str, Any]:
    """Detect late entries without treating exhaustion as a reversal signal."""
    signal = candidate.get("signal") or {}
    smart = candidate.get("smart_flow") or {}
    direction = str(candidate.get("direction") or "").upper()
    extension = max(0.0, float(signal.get("breakout_extension_atr") or 0.0))
    impulse = max(0.0, float(signal.get("impulse_atr") or 0.0))
    wick = max(0.0, float(signal.get("adverse_wick_ratio") or 0.0))
    volume = max(0.0, float(signal.get("volume_acceleration") or 1.0))
    path = max(0.0, float((market_structure(candidate) or {}).get("medium_path_efficiency") or 0.0))
    alignment = _clamp(float(smart.get("directional_alignment") or 0.0), -1.0, 1.0)
    confidence = _clamp(float(smart.get("confidence") or 0.0), 0.0, 1.0)
    against = -alignment if direction == "LONG" else alignment
    components = {
        "extension": _clamp(extension / max(float(config.get("opportunity_v48_exhaustion_extension_atr", 0.85)), 0.01), 0.0, 1.0),
        "impulse": _clamp(impulse / max(float(config.get("opportunity_v48_exhaustion_impulse_atr", 1.60)), 0.01), 0.0, 1.0),
        "adverse_wick": _clamp(wick / max(float(config.get("opportunity_v48_exhaustion_wick_ratio", 2.50)), 0.01), 0.0, 1.0),
        "volume_decay": _clamp((float(config.get("opportunity_v48_exhaustion_volume_floor", 1.05)) - volume) / 0.45, 0.0, 1.0),
        "path_failure": _clamp((float(config.get("opportunity_v48_exhaustion_path_floor", 0.18)) - path) / 0.18, 0.0, 1.0),
        "smart_divergence": _clamp(against * confidence, 0.0, 1.0),
    }
    score = (
        components["extension"] * 0.25
        + components["impulse"] * 0.15
        + components["adverse_wick"] * 0.20
        + components["volume_decay"] * 0.15
        + components["path_failure"] * 0.15
        + components["smart_divergence"] * 0.10
    )
    enabled = bool(config.get("opportunity_v48_exhaustion_enabled", True))
    hard = score >= float(config.get("opportunity_v48_exhaustion_block_score", 0.62))
    caution = score >= float(config.get("opportunity_v48_exhaustion_caution_score", 0.42))
    return {
        "enabled": enabled,
        "score": round(score, 6),
        "blocked": bool(enabled and hard),
        "caution": bool(enabled and caution),
        "risk_multiplier": float(config.get("opportunity_v48_exhaustion_caution_multiplier", 0.65)) if enabled and caution else 1.0,
        "components": {key: round(value, 6) for key, value in components.items()},
        "reason": "late_trend_exhaustion" if hard else "exhaustion_caution" if caution else "structure_healthy",
    }


def _v48_evidence_policy(local_circuit: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Turn exact-cohort evidence into a bounded candidate action, not a global pause."""
    if not config.get("opportunity_v48_local_evidence_enabled", True):
        return {"level": "clear", "blocked": False, "risk_multiplier": 1.0, "threshold_delta": 0.0}
    reason = str(local_circuit.get("reason") or "local_circuit_clear")
    live_streak = int(local_circuit.get("live_loss_streak") or 0)
    trigger = ((local_circuit.get("shadow") or {}).get("trigger") or {})
    trades = int(trigger.get("trades") or 0)
    pf = float(trigger.get("profit_factor") or 0.0)
    severe_shadow = bool(
        reason == "local_shadow_negative"
        and trades >= int(config.get("opportunity_v48_evidence_hard_min_trades", 20))
        and pf < float(config.get("opportunity_v48_evidence_hard_pf", 0.55))
    )
    # Re-entry owns live-loss handling because a genuinely new structure may
    # reset the sequence. Shadow evidence remains version/cohort isolated.
    hard = severe_shadow
    caution = bool(local_circuit.get("blocked") or live_streak > 0)
    return {
        "level": "hard" if hard else "caution" if caution else "clear",
        "blocked": hard,
        "risk_multiplier": 0.0 if hard else float(config.get("opportunity_v48_evidence_caution_multiplier", 0.60)) if caution else 1.0,
        "threshold_delta": float(config.get("opportunity_v48_evidence_caution_quality_delta", 0.04)) if caution else 0.0,
        "reason": reason,
        "live_loss_streak": live_streak,
        "shadow_trigger_trades": trades,
        "shadow_trigger_pf": pf,
    }


def _v48_reentry_policy(
    candidate: dict[str, Any],
    features: dict[str, float],
    local_circuit: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Require a structural reset before repeatedly entering a losing cohort."""
    if not config.get("opportunity_v48_reentry_enabled", True):
        return {"state": "clear", "blocked": False, "risk_multiplier": 1.0, "structural_reset": False}
    streak = int(local_circuit.get("live_loss_streak") or 0)
    signal = candidate.get("signal") or {}
    phase = _entry_phase(candidate)
    volume = max(0.0, float(signal.get("volume_acceleration") or 1.0))
    extension = max(0.0, float(signal.get("breakout_extension_atr") or 0.0))
    structural_reset = bool(
        phase == "RETEST"
        and features.get("medium_path", 0.0) >= float(config.get("opportunity_v48_reentry_reset_path", 0.25))
        and volume >= float(config.get("opportunity_v48_reentry_reset_volume", 1.05))
        and extension <= float(config.get("opportunity_v48_reentry_reset_extension_atr", 0.35))
    )
    hard_losses = int(config.get("opportunity_v48_reentry_hard_losses", 2))
    blocked = bool(streak >= hard_losses and not structural_reset)
    caution = bool(streak > 0 and not structural_reset and not blocked)
    return {
        "state": "reset" if structural_reset else "blocked" if blocked else "caution" if caution else "clear",
        "blocked": blocked,
        "risk_multiplier": (
            0.0
            if blocked
            else float(config.get("opportunity_v48_reentry_caution_multiplier", 0.70))
            if caution
            else 1.0
        ),
        "structural_reset": structural_reset,
        "live_loss_streak": streak,
        "entry_phase": phase,
        "medium_path": round(float(features.get("medium_path") or 0.0), 6),
        "volume_acceleration": round(volume, 6),
        "breakout_extension_atr": round(extension, 6),
    }


def _model_expectancy(candidate: dict[str, Any], features: dict[str, float], config: dict[str, Any]) -> dict[str, float]:
    base_quality = sum(features[name] * weight for name, weight in V462_FEATURE_WEIGHTS.items())
    structure = market_structure(candidate)
    setup_type = normalize_setup_type(structure.get("setup_type") or candidate.get("entry_type"))
    setup_adjustment = V462_SETUP_ADJUSTMENTS.get(setup_type, 0.0)
    smart_points = float(candidate.get("smart_flow_score_delta") or 0.0)
    version = str(config.get("opportunity_v4_strategy_version") or "").lower()
    if version.startswith(("v4.8", "v4.9")):
        smart = candidate.get("smart_flow") or {}
        confirmed = bool(
            smart.get("available")
            and float(smart.get("confidence") or 0.0) >= float(config.get("smart_flow_min_confidence", 0.45))
            and features["anti_chase"] >= 0.45
            and features["medium_path"] >= 0.35
        )
        smart_points = _clamp(
            smart_points if confirmed else 0.0,
            -float(config.get("opportunity_v48_smart_flow_max_points", 2.0)),
            float(config.get("opportunity_v48_smart_flow_max_points", 2.0)),
        )
    quality = _clamp(base_quality + setup_adjustment + smart_points / 100.0, 0.0, 1.0)
    signal = candidate.get("signal") or {}
    entry = max(float(signal.get("last_price") or 0), 0.00000001)
    stop = float(signal.get("stop") or 0)
    stop_pct = abs(entry - stop) / entry * 100 if stop > 0 else 1.0
    reward_pct = float(candidate.get("expected_profit_pct") or signal.get("expected_profit_pct") or 0)
    cost_pct = _dynamic_cost_pct(candidate, config)
    win_probability = _clamp(0.28 + quality * 0.36, 0.30, 0.62)
    expected = win_probability * reward_pct - (1.0 - win_probability) * stop_pct - cost_pct
    uncertainty = 0.04 + (1.0 - quality) * 0.12
    return {
        "quality": quality,
        "quality_without_smart": _clamp(base_quality + setup_adjustment, 0.0, 1.0),
        "base_quality": base_quality,
        "setup_adjustment": setup_adjustment,
        "smart_flow_adjustment": smart_points / 100.0,
        "win_probability": win_probability,
        "expected_net_pct": expected,
        "lower_expected_net_pct": expected - uncertainty,
        "uncertainty_pct": uncertainty,
        "stop_pct": stop_pct,
        "reward_pct": reward_pct,
        "cost_pct": cost_pct,
        "cost_ratio": reward_pct / cost_pct if cost_pct > 0 else 999.0,
    }


def _liquidity_gate(
    candidate: dict[str, Any],
    config: dict[str, Any],
    risk_multiplier: float,
) -> dict[str, Any]:
    """Scale depth requirements to the notional this admission lane would send."""
    depth = candidate.get("depth") or {}
    execution = candidate.get("execution_filter") or {}
    signal = candidate.get("signal") or {}
    spread_raw = depth.get("spread_pct")
    spread_pct = float(spread_raw) if spread_raw is not None else 999.0
    depth_notional = max(float(depth.get("depth_notional") or 0.0), 0.0)
    entry = max(float(signal.get("last_price") or 0.0), 0.0)
    raw_notional = max(float(execution.get("raw_quantity") or 0.0) * entry, 0.0)
    cap_notional = max(float(execution.get("max_quantity") or 0.0) * entry, 0.0)
    reported_notional = max(float(execution.get("notional") or 0.0), 0.0)
    if raw_notional > 0:
        order_notional = raw_notional * max(risk_multiplier, 0.0)
        if cap_notional > 0:
            order_notional = min(order_notional, cap_notional)
    else:
        order_notional = reported_notional * max(risk_multiplier, 0.0)
    order_notional = max(order_notional, float(execution.get("min_notional") or 0.0))

    max_spread_pct = float(
        config.get("execution_max_spread_pct", config.get("opportunity_v3_max_spread_pct", 0.10))
    )
    if not config.get("opportunity_v43_dynamic_liquidity_enabled", True):
        required_depth = float(
            config.get(
                "execution_min_depth_notional_usdt",
                config.get("opportunity_v3_min_depth_notional_usdt", 5_000.0),
            )
        )
        max_book_share_pct = 100.0
    else:
        depth_floor = float(config.get("opportunity_v43_min_depth_floor_usdt", 750.0))
        depth_multiple = float(config.get("opportunity_v43_depth_to_order_multiple", 12.5))
        max_required_depth = float(config.get("opportunity_v43_max_required_depth_usdt", 50_000.0))
        required_depth = min(max(depth_floor, order_notional * depth_multiple), max_required_depth)
        max_book_share_pct = float(config.get("opportunity_v43_max_order_book_share_pct", 8.0))
    book_share_pct = order_notional / depth_notional * 100 if depth_notional > 0 else 999.0
    passed = bool(
        spread_pct <= max_spread_pct
        and depth_notional >= required_depth
        and book_share_pct <= max_book_share_pct
    )
    return {
        "passed": passed,
        "spread_pct": round(spread_pct, 6),
        "max_spread_pct": round(max_spread_pct, 6),
        "depth_notional": round(depth_notional, 4),
        "required_depth_notional": round(required_depth, 4),
        "estimated_order_notional": round(order_notional, 4),
        "order_book_share_pct": round(book_share_pct, 4),
        "max_order_book_share_pct": round(max_book_share_pct, 4),
        "risk_multiplier": round(risk_multiplier, 4),
    }


def _regime_policy(candidate: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    version_label = str(config.get("opportunity_v4_strategy_version") or "v4.3.2").upper()
    regime = _market_regime(candidate)
    direction = str(candidate.get("direction") or "").upper()
    structure = market_structure(candidate)
    setup_type = normalize_setup_type(structure.get("setup_type") or candidate.get("entry_type"))
    phase = _entry_phase(candidate)
    aligned = bool(structure.get("medium_trend_aligned"))
    regime_aligned = (regime == "broad_down" and direction == "SHORT") or (
        regime == "broad_up" and direction == "LONG"
    )
    trend_aligned = aligned or regime_aligned
    if regime == "panic":
        return {
            "scope": "panic_shadow_only",
            "live_scope": False,
            "canary_scope": False,
            "exploration_scope": False,
            "trend_aligned": trend_aligned,
            "reason": "恐慌行情只记录影子，不在失序盘口追价",
        }
    v44_active = bool(
        str(config.get("opportunity_v4_strategy_version") or "").lower().startswith(("v4.4", "v4.5", "v4.6", "v4.7", "v4.8", "v4.9"))
        and config.get("opportunity_v44_full_bet_enabled", True)
    )
    v44_structure = bool(
        (setup_type == "pullback" and phase == "RETEST")
        or setup_type in {"momentum", "prebreakout"}
    )
    if v44_active and trend_aligned and v44_structure and regime in {"quiet", "broad_up", "broad_down", "rotation"}:
        return {
            "scope": "v44_trend_aligned_full_bet",
            "live_scope": True,
            "canary_scope": True,
            "exploration_scope": False,
            "trend_aligned": True,
            "reason": f"{version_label} 当前方向与中周期趋势一致，进入全仓短打候选通道",
        }
    if regime == "quiet" and direction == "LONG" and aligned and setup_type == "pullback" and phase == "RETEST":
        return {
            "scope": "quiet_long_pullback_core",
            "live_scope": True,
            "canary_scope": True,
            "exploration_scope": False,
            "trend_aligned": True,
            "reason": "静市做多回踩且中周期对齐，进入核心试运行通道",
        }
    if regime == "quiet" and aligned and (
        setup_type in {"momentum", "prebreakout"}
        or (direction == "SHORT" and setup_type == "breakout")
    ):
        return {
            "scope": "quiet_aligned_exploration",
            "live_scope": False,
            "canary_scope": False,
            "exploration_scope": True,
            "trend_aligned": True,
            "reason": "静市中周期对齐的动量或突破进入受限探索通道",
        }
    if regime == "mixed" and direction == "LONG" and aligned and setup_type in {
        "momentum",
        "prebreakout",
        "breakout",
    }:
        return {
            "scope": "mixed_long_aligned_exploration",
            "live_scope": False,
            "canary_scope": False,
            "exploration_scope": True,
            "trend_aligned": True,
            "reason": "混合行情做多且中周期对齐，进入受限探索通道",
        }
    if (
        regime == "broad_up"
        and direction == "LONG"
        and aligned
        and setup_type in {"pullback", "breakout"}
        and phase in {"RETEST", "ARMED"}
    ):
        return {
            "scope": "broad_up_long_core",
            "live_scope": True,
            "canary_scope": True,
            "exploration_scope": False,
            "trend_aligned": True,
            "reason": "广泛上涨中做多且中周期对齐，进入核心试运行通道",
        }
    if regime == "broad_up" and direction == "LONG" and aligned and setup_type in {
        "pullback",
        "breakout",
        "momentum",
        "prebreakout",
    }:
        return {
            "scope": "broad_up_long_exploration",
            "live_scope": False,
            "canary_scope": False,
            "exploration_scope": True,
            "trend_aligned": True,
            "reason": "广泛上涨中做多且中周期对齐，先进入受限探索通道",
        }
    if regime == "rotation" and aligned and (
        (setup_type == "pullback" and phase == "RETEST") or setup_type in {"momentum", "prebreakout"}
    ):
        return {
            "scope": "rotation_aligned_exploration",
            "live_scope": False,
            "canary_scope": False,
            "exploration_scope": True,
            "trend_aligned": True,
            "reason": "轮动行情中周期对齐，进入受限探索通道",
        }
    if regime == "broad_down" and direction == "SHORT":
        return {
            "scope": "broad_down_short_shadow_only",
            "live_scope": False,
            "canary_scope": False,
            "exploration_scope": False,
            "trend_aligned": trend_aligned,
            "reason": f"V4.2 历史样本显示广泛下跌追空为负期望，{version_label} 当前结构未达实盘通道",
        }
    return {
        "scope": "shadow_only",
        "live_scope": False,
        "canary_scope": False,
        "exploration_scope": False,
        "trend_aligned": trend_aligned,
        "reason": f"方向或结构未达到 {version_label} 实盘通道，继续积累当前版本影子证据",
    }


def _protection_profile(candidate: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    version = str(config.get("opportunity_v4_strategy_version") or "v4.3.2").lower()
    if version.startswith(("v4.4", "v4.5", "v4.6", "v4.7", "v4.8", "v4.9")) and config.get("opportunity_v44_full_bet_enabled", True):
        stop_atr = float(config.get("opportunity_v44_stop_atr", 0.85))
        take_profit_r = float(config.get("opportunity_v44_take_profit_r", 1.05))
        return {
            "entry_phase": _entry_phase(candidate),
            "profile": "s0_full_bet_v44",
            "stop_atr": stop_atr,
            "take_profit_atr": stop_atr * take_profit_r,
            "max_hold_bars": int(config.get("opportunity_v44_max_hold_bars", 2)),
            "fast_invalid_atr": stop_atr * 0.45,
            "break_even_trigger_atr": stop_atr
            * float(config.get("opportunity_v44_break_even_trigger_r", 0.45)),
            "trailing_trigger_atr": stop_atr * 0.85,
            "trailing_distance_atr": stop_atr * 0.50,
        }
    phase = _entry_phase(candidate)
    prefix = "retest" if phase == "RETEST" else "armed" if phase == "ARMED" else "triggered"
    return {
        "entry_phase": phase,
        "stop_atr": float(config.get(f"opportunity_v41_{prefix}_stop_atr", 0.70)),
        "take_profit_atr": float(config.get(f"opportunity_v41_{prefix}_take_profit_atr", 1.20)),
        "max_hold_bars": int(config.get(f"opportunity_v41_{prefix}_max_hold_bars", 8)),
    }


def continuous_position_confidence(opportunity: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Map independent V4 evidence to a continuous initial-risk target.

    This deliberately avoids recreating the old A+/A/B buckets. The display
    label is explanatory only; execution uses the numeric confidence and keeps
    the existing direction-evidence multiplier.
    """
    enabled = bool(config.get("opportunity_v432_continuous_sizing_enabled", True))
    lane = str(opportunity.get("admission_lane") or "shadow_only")
    core_lane = lane in {"validated", "core_provisional", "core_canary"}
    admitted = bool(opportunity.get("admitted"))
    if not enabled or not core_lane or not admitted:
        return {
            "enabled": enabled,
            "applied": False,
            "method": "continuous_v432",
            "lane": lane,
            "confidence": 0.0,
            "display_label": "受限探索" if lane == "limited_exploration" else "仅影子",
            "target_initial_risk_pct": None,
            "add_on_eligible": False,
            "reason": "连续质量仓位仅作用于已经通过准入的核心通道",
        }

    def clamp(value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    rank = float(opportunity.get("rank_percentile") or 0.0)
    lower_expected = float(opportunity.get("lower_expected_net_pct") or 0.0)
    cost = max(float(opportunity.get("estimated_cost_pct") or 0.0), 0.000001)
    cost_ratio = float(opportunity.get("cost_ratio") or 0.0)
    features = opportunity.get("features") or {}
    policy = opportunity.get("regime_policy") or {}
    liquidity = opportunity.get("liquidity_gate") or {}
    local_circuit = opportunity.get("local_circuit") or {}

    rank_floor = float(config.get("opportunity_v432_confidence_rank_floor", 0.75))
    rank_component = clamp((rank - rank_floor) / max(1.0 - rank_floor, 0.000001))
    conservative_cost_multiple = lower_expected / cost
    lower_component = clamp(
        (conservative_cost_multiple - float(config.get("opportunity_v432_confidence_lower_cost_floor", 0.5)))
        / max(
            float(config.get("opportunity_v432_confidence_lower_cost_full", 2.5))
            - float(config.get("opportunity_v432_confidence_lower_cost_floor", 0.5)),
            0.000001,
        )
    )
    cost_component = clamp(
        (cost_ratio - float(config.get("opportunity_v432_confidence_cost_ratio_floor", 2.0)))
        / max(
            float(config.get("opportunity_v432_confidence_cost_ratio_full", 10.0))
            - float(config.get("opportunity_v432_confidence_cost_ratio_floor", 2.0)),
            0.000001,
        )
    )
    medium_aligned = bool(float(features.get("medium_alignment") or 0.0) >= 0.5 or policy.get("trend_aligned"))
    liquidity_passed = bool(liquidity.get("passed"))
    circuit_clear = not bool(local_circuit.get("blocked"))
    components = {
        "rank": rank_component,
        "conservative_net_after_cost": lower_component,
        "cost_efficiency": cost_component,
        "medium_alignment": 1.0 if medium_aligned else 0.0,
        "liquidity": 1.0 if liquidity_passed else 0.0,
        "local_circuit": 1.0 if circuit_clear else 0.0,
    }
    weights = {
        "rank": 0.30,
        "conservative_net_after_cost": 0.25,
        "cost_efficiency": 0.15,
        "medium_alignment": 0.15,
        "liquidity": 0.10,
        "local_circuit": 0.05,
    }
    confidence = clamp(sum(components[key] * weights[key] for key in weights))
    minimum_risk = float(config.get("opportunity_v432_initial_min_risk_pct", 4.0))
    maximum_risk = max(minimum_risk, float(config.get("opportunity_v432_initial_max_risk_pct", 7.5)))
    evidence_multiplier = max(0.0, min(1.0, float(opportunity.get("evidence_risk_multiplier") or 1.0)))
    target = (minimum_risk + (maximum_risk - minimum_risk) * confidence) * evidence_multiplier
    add_on_eligible = bool(
        confidence >= float(config.get("opportunity_v432_add_on_min_confidence", 0.75))
        and conservative_cost_multiple >= float(config.get("opportunity_v432_add_on_min_lower_cost_multiple", 2.0))
        and cost_ratio >= float(config.get("opportunity_v432_add_on_min_cost_ratio", 8.0))
        and medium_aligned
        and liquidity_passed
        and circuit_clear
    )
    return {
        "enabled": True,
        "applied": True,
        "method": "continuous_v432",
        "lane": lane,
        "confidence": round(confidence, 6),
        "display_label": "强机会" if confidence >= 0.80 else "核心机会",
        "components": {key: round(value, 6) for key, value in components.items()},
        "conservative_cost_multiple": round(conservative_cost_multiple, 6),
        "evidence_multiplier": round(evidence_multiplier, 6),
        "target_initial_risk_pct": round(target, 6),
        "configured_initial_range_pct": [round(minimum_risk, 6), round(maximum_risk, 6)],
        "add_on_eligible": add_on_eligible,
        "add_on_trigger_atr": float(config.get("opportunity_v432_add_on_trigger_atr", 0.55)),
        "add_on_total_risk_cap_pct": float(config.get("opportunity_v432_add_on_total_risk_cap_pct", 15.0)),
        "reason": "核心机会按连续置信度计算初始风险；显示标签不参与硬分层",
    }


def v44_position_confidence(opportunity: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    version_label = str(config.get("opportunity_v4_strategy_version") or "v4.9").upper()
    lane = str(opportunity.get("admission_lane") or "shadow_only")
    admitted = bool(opportunity.get("admitted"))
    if lane != "full_bet" or not admitted:
        return {
            "enabled": True,
            "applied": False,
            "method": "s0_full_bet_v44",
            "lane": lane,
            "confidence": 0.0,
            "display_label": "仅影子观察",
            "target_initial_risk_pct": None,
            "add_on_eligible": False,
            "reason": f"{version_label} 只对通过相对排名和三重确认的 S0 候选计算全仓风险",
        }

    rank_floor = float(config.get("opportunity_v44_min_rank_percentile", 0.80))
    rank = float(opportunity.get("rank_percentile") or 0.0)
    rank_component = _clamp((rank - rank_floor) / max(1.0 - rank_floor, 0.000001), 0.0, 1.0)
    confirmations = int(opportunity.get("v44_confirmations") or 0)
    required = int(config.get("opportunity_v44_min_confirmations", 3))
    confirmation_component = _clamp((confirmations - required) / max(5 - required, 1), 0.0, 1.0)
    cost_ratio = float(opportunity.get("cost_ratio") or 0.0)
    cost_floor = float(config.get("opportunity_v44_min_cost_ratio", 1.50))
    cost_component = _clamp((cost_ratio - cost_floor) / max(8.0 - cost_floor, 0.000001), 0.0, 1.0)
    lower = float(opportunity.get("lower_expected_net_pct") or 0.0)
    lower_floor = float(config.get("opportunity_v44_min_lower_expectancy_pct", -0.05))
    lower_component = _clamp((lower - lower_floor) / max(0.15 - lower_floor, 0.000001), 0.0, 1.0)
    liquidity_component = 1.0 if (opportunity.get("liquidity_gate") or {}).get("passed") else 0.0
    confidence = _clamp(
        rank_component * 0.35
        + confirmation_component * 0.25
        + cost_component * 0.20
        + lower_component * 0.10
        + liquidity_component * 0.10,
        0.0,
        1.0,
    )
    minimum_risk = float(config.get("opportunity_v44_min_risk_pct", 8.0))
    maximum_risk = max(minimum_risk, float(config.get("opportunity_v44_max_risk_pct", 15.0)))
    calibration = opportunity.get("adaptive_calibration") or {}
    direction = str(opportunity.get("direction") or "LONG").upper()
    if float(opportunity.get("risk_multiplier") or 0.0) > 0:
        direction_risk_multiplier = float(opportunity["risk_multiplier"])
    elif calibration.get("enabled"):
        direction_risk_multiplier = float(calibration.get("risk_multiplier") or 1.0)
    elif str(config.get("opportunity_v4_strategy_version") or "").lower().startswith("v4.6.2") and direction == "SHORT":
        direction_risk_multiplier = float(config.get("opportunity_v462_short_risk_multiplier", 0.65))
    else:
        direction_risk_multiplier = 1.0
    stressed_cap = float(config.get("opportunity_v44_stressed_risk_cap_pct", 15.0))
    target = min(
        maximum_risk,
        stressed_cap,
        (minimum_risk + (maximum_risk - minimum_risk) * confidence) * direction_risk_multiplier,
    )
    return {
        "enabled": True,
        "applied": True,
        "method": "s0_full_bet_v44",
        "lane": lane,
        "confidence": round(confidence, 6),
        "display_label": "全仓强机会" if confidence >= 0.65 else "全仓机会",
        "components": {
            "relative_rank": round(rank_component, 6),
            "confirmations": round(confirmation_component, 6),
            "cost_efficiency": round(cost_component, 6),
            "conservative_expectancy": round(lower_component, 6),
            "liquidity": round(liquidity_component, 6),
            "direction_risk": round(direction_risk_multiplier, 6),
        },
        "target_initial_risk_pct": round(target, 6),
        "configured_initial_range_pct": [round(minimum_risk, 6), round(maximum_risk, 6)],
        "add_on_eligible": False,
        "adaptive_calibration": calibration,
        "reason": "按本轮相对排名、五项确认、成本效率和流动性连续计算 8%-15% 计划风险",
    }
def attach_v4_rankings(
    candidates: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    reuse_existing_context: bool = False,
) -> list[dict[str, Any]]:
    if not config.get("opportunity_v4_enabled", True):
        return candidates
    prepared: list[tuple[dict[str, Any], dict[str, float], dict[str, float]]] = []
    for candidate in candidates:
        signal = candidate.get("signal") or {}
        direction = str(candidate.get("direction") or "").upper()
        if direction not in {"LONG", "SHORT"} or signal.get("signal") != direction:
            continue
        features = _model_features(candidate)
        prepared.append((candidate, features, _model_expectancy(candidate, features, config)))

    model_values = [item[2]["expected_net_pct"] for item in prepared]
    provisional_samples = int(config.get("opportunity_v4_admission_min_trades", 200))
    provisional_pf = float(config.get("opportunity_v4_admission_min_profit_factor", 1.10))
    min_lower = float(config.get("opportunity_v4_admission_min_lower_expectancy_pct", 0.0))
    validated_samples = int(config.get("opportunity_v41_validation_min_trades", 500))
    validated_pf = float(config.get("opportunity_v41_validation_min_profit_factor", 1.15))
    min_time_blocks = int(config.get("opportunity_v41_validation_min_time_blocks", 2))
    min_symbols = int(config.get("opportunity_v41_validation_min_symbols", 3))
    prior_trades = float(config.get("opportunity_v41_empirical_prior_trades", 40))
    version = str(config.get("opportunity_v4_strategy_version") or "v4.3.2")
    v44_active = bool(version.lower().startswith(("v4.4", "v4.5", "v4.6", "v4.7", "v4.8", "v4.9")) and config.get("opportunity_v44_full_bet_enabled", True))
    v48_active = bool(version.lower().startswith(("v4.8", "v4.9")))
    v44_rank = float(config.get("opportunity_v44_min_rank_percentile", 0.80))
    v44_quality = float(config.get("opportunity_v44_min_quality_score", 52.0)) / 100
    v44_expected = float(config.get("opportunity_v44_min_expected_net_pct", 0.02))
    v44_lower = float(config.get("opportunity_v44_min_lower_expectancy_pct", -0.05))
    v44_cost_ratio = float(config.get("opportunity_v44_min_cost_ratio", 1.50))
    v44_confirmations_required = int(config.get("opportunity_v44_min_confirmations", 3))
    if v48_active:
        v44_quality = float(config.get("opportunity_v48_min_quality_score", 56.0)) / 100
        v44_expected = float(config.get("opportunity_v48_min_expected_net_pct", 0.03))
        v44_lower = float(config.get("opportunity_v48_min_lower_expectancy_pct", -0.03))
        v44_cost_ratio = float(config.get("opportunity_v48_min_cost_ratio", 1.70))
    decision_limit = int(config.get("opportunity_v4_decision_shadow_limit", 3))
    bootstrap_rank = float(config.get("opportunity_v4_bootstrap_min_rank_percentile", 0.85))
    bootstrap_quality = float(config.get("opportunity_v4_bootstrap_min_quality_score", 58.0)) / 100
    min_expected = max(
        float(config.get("opportunity_v41_min_expected_net_pct", 0.10)),
        float(config.get("opportunity_v4_bootstrap_min_model_expectancy_pct", 0.10)),
    )
    min_cost_ratio = float(config.get("opportunity_v41_min_cost_ratio", 2.0))
    require_alignment = bool(config.get("opportunity_v41_medium_alignment_required", True))
    live_enabled = bool(config.get("opportunity_v4_live_enabled", False))
    exploration_enabled = bool(
        config.get("opportunity_v43_exploration_enabled", config.get("opportunity_v42_exploration_enabled", True))
    )
    exploration_rank = float(
        config.get(
            "opportunity_v43_exploration_min_rank_percentile",
            config.get("opportunity_v42_exploration_min_rank_percentile", 0.75),
        )
    )
    exploration_quality = float(
        config.get(
            "opportunity_v43_exploration_min_quality_score",
            config.get("opportunity_v42_exploration_min_quality_score", 55.0),
        )
    ) / 100
    exploration_expected = float(
        config.get(
            "opportunity_v43_exploration_min_expected_net_pct",
            config.get("opportunity_v42_exploration_min_expected_net_pct", 0.04),
        )
    )
    exploration_lower = float(
        config.get(
            "opportunity_v43_exploration_min_lower_expectancy_pct",
            config.get("opportunity_v42_exploration_min_lower_expectancy_pct", -0.03),
        )
    )
    exploration_cost_ratio = float(
        config.get(
            "opportunity_v43_exploration_min_cost_ratio",
            config.get("opportunity_v42_exploration_min_cost_ratio", 1.60),
        )
    )
    exploration_confirmations_required = int(
        config.get(
            "opportunity_v43_exploration_min_confirmations",
            config.get("opportunity_v42_exploration_min_confirmations", 2),
        )
    )
    validated_risk = float(config.get("opportunity_v4_validated_risk_multiplier", 1.0))
    provisional_risk = float(config.get("opportunity_v41_provisional_risk_multiplier", 0.70))
    bootstrap_risk = float(config.get("opportunity_v4_bootstrap_risk_multiplier", 0.40))
    exploration_risk = float(
        config.get(
            "opportunity_v43_exploration_risk_multiplier",
            config.get("opportunity_v42_exploration_risk_multiplier", 0.40),
        )
    )
    ranked: list[tuple[float, dict[str, Any]]] = []
    circuit_state = local_circuit_state(f"{V4_STRATEGY_FAMILY}@{version}")
    loaded_evidence = [] if reuse_existing_context else evidence_rows(config)

    for candidate, features, model in prepared:
        rank = _percentile_rank(model_values, model["expected_net_pct"])
        existing_opportunity = candidate.get("opportunity_v4") or {}
        evidence = (
            existing_opportunity.get("evidence")
            if reuse_existing_context and existing_opportunity.get("evidence")
            else _cohort_evidence(candidate, config)
        )
        selected = evidence["selected"]
        hierarchical = evidence["hierarchical"]
        empirical_weight = float(hierarchical["local_trades"]) / (
            float(hierarchical["local_trades"]) + max(prior_trades, 1.0)
        )
        expected = model["expected_net_pct"] * (1.0 - empirical_weight) + float(
            hierarchical["expected_net_pct"]
        ) * empirical_weight
        lower = model["lower_expected_net_pct"] * (1.0 - empirical_weight) + float(
            hierarchical["lower_expected_net_pct"]
        ) * empirical_weight
        execution = candidate.get("execution_filter") or {}
        executable = not execution.get("enabled") or bool(execution.get("executable"))
        policy = _regime_policy(candidate, config)
        calibration = adaptive_calibration(candidate, config) if version.lower().startswith(("v4.7", "v4.8", "v4.9")) else {
            "enabled": False,
            "relation": "legacy",
            "risk_multiplier": 1.0,
            "rank_threshold_delta": 0.0,
            "expectancy_threshold_delta_pct": 0.0,
            "confirmation_delta": 0,
        }
        effective_v44_rank = _clamp(
            v44_rank + float(calibration.get("rank_threshold_delta") or 0.0), 0.0, 1.0
        )
        effective_v44_expected = v44_expected + float(
            calibration.get("expectancy_threshold_delta_pct") or 0.0
        )
        effective_v44_confirmations = max(
            2,
            min(5, v44_confirmations_required + int(calibration.get("confirmation_delta") or 0)),
        )
        structure = market_structure(candidate)
        setup_type = normalize_setup_type(structure.get("setup_type") or candidate.get("entry_type"))
        medium_aligned = bool(structure.get("medium_trend_aligned"))
        alignment_ok = medium_aligned or bool(policy["trend_aligned"]) or not require_alignment
        cost_ok = model["cost_ratio"] >= min_cost_ratio
        blended_model_ok = expected >= min_expected and lower > min_lower
        shadow_eligible = bool(setup_type != "unknown" and model["reward_pct"] > model["cost_pct"])
        evidence_risk_multiplier = 1.0 if v44_active else _direction_evidence_multiplier(evidence, config)
        local_circuit = (
            existing_opportunity.get("local_circuit")
            if reuse_existing_context and existing_opportunity.get("local_circuit")
            else candidate_local_circuit_status(
                candidate,
                loaded_evidence,
                config,
                state=circuit_state,
            )
        )
        negative_evidence = bool(local_circuit.get("blocked"))
        exhaustion = _v48_exhaustion(candidate, features, config) if v48_active else {
            "enabled": False, "score": 0.0, "blocked": False, "caution": False,
            "risk_multiplier": 1.0, "components": {}, "reason": "legacy",
        }
        evidence_policy = _v48_evidence_policy(local_circuit, config) if v48_active else {
            "level": "legacy", "blocked": False, "risk_multiplier": 1.0, "threshold_delta": 0.0,
        }
        reentry_policy = _v48_reentry_policy(candidate, features, local_circuit, config) if v48_active else {
            "state": "legacy", "blocked": False, "risk_multiplier": 1.0, "structural_reset": False,
        }
        validated_liquidity = _liquidity_gate(candidate, config, validated_risk * evidence_risk_multiplier)
        provisional_liquidity = _liquidity_gate(candidate, config, provisional_risk * evidence_risk_multiplier)
        canary_liquidity = _liquidity_gate(candidate, config, bootstrap_risk * evidence_risk_multiplier)
        exploration_liquidity = _liquidity_gate(candidate, config, exploration_risk * evidence_risk_multiplier)
        adaptive_risk_multiplier = float(calibration.get("risk_multiplier") or 1.0)
        full_bet_liquidity = _liquidity_gate(candidate, config, adaptive_risk_multiplier)
        common_gates = executable and alignment_ok and cost_ok and blended_model_ok and not negative_evidence
        validated = bool(
            live_enabled
            and policy["live_scope"]
            and rank >= float(config.get("opportunity_v4_decision_min_rank_percentile", 0.75))
            and selected["trades"] >= validated_samples
            and selected["profit_factor"] >= validated_pf
            and selected["lower_expected_net_pct"] > min_lower
            and selected["time_blocks"] >= min_time_blocks
            and selected["symbols"] >= min_symbols
            and common_gates
            and validated_liquidity["passed"]
        )
        provisional = bool(
            live_enabled
            and policy["live_scope"]
            and rank >= bootstrap_rank
            and selected["trades"] >= provisional_samples
            and selected["profit_factor"] >= provisional_pf
            and selected["net_pct"] > 0
            and selected["lower_expected_net_pct"] > min_lower
            and common_gates
            and provisional_liquidity["passed"]
            and not validated
        )
        canary_eligible = bool(
            live_enabled
            and config.get("opportunity_v4_bootstrap_enabled", True)
            and policy["canary_scope"]
            and rank >= bootstrap_rank
            and model["quality"] >= bootstrap_quality
            and common_gates
            and canary_liquidity["passed"]
            and not validated
            and not provisional
        )
        momentum_confirmations = sum(
            (
                features["volume_persistence"] >= 0.60,
                features["directed_flow"] >= 0.60,
                features["cross_sectional_strength"] >= 0.75,
                features["medium_path"] >= 0.45,
            )
        )
        v44_confirmations = sum(
            (
                features["volume_persistence"] >= 0.55,
                features["directed_flow"] >= 0.70,
                features["regime_fit"] >= 0.80,
                features["medium_path"] >= 0.45 or bool(policy["trend_aligned"]),
                features["anti_chase"] >= 0.60,
            )
        )
        direction = str(candidate.get("direction") or "").upper()
        if version.lower().startswith(("v4.7", "v4.8", "v4.9")):
            direction_quality_ok = bool(
                calibration.get("relation") != "countertrend"
                or (
                    features["directed_flow"] >= float(config.get("opportunity_v47_countertrend_min_directed_flow", 0.72))
                    and features["regime_fit"] >= float(config.get("opportunity_v47_countertrend_min_regime_fit", 0.85))
                    and features["medium_path"] >= float(config.get("opportunity_v47_countertrend_min_medium_path", 0.45))
                    and bool(policy["trend_aligned"])
                )
            )
        else:
            direction_quality_ok = bool(
                direction != "SHORT"
                or (
                    features["directed_flow"] >= float(config.get("opportunity_v462_short_min_directed_flow", 0.72))
                    and features["regime_fit"] >= float(config.get("opportunity_v462_short_min_regime_fit", 0.85))
                    and features["medium_path"] >= float(config.get("opportunity_v462_short_min_medium_path", 0.45))
                    and bool(policy["trend_aligned"])
                )
            )
        momentum_confirmed = setup_type in {"momentum", "prebreakout"} and (
            momentum_confirmations >= exploration_confirmations_required
        )
        structured_entry_confirmed = (
            setup_type == "pullback" and _entry_phase(candidate) == "RETEST" and bool(policy["trend_aligned"])
        ) or (
            setup_type == "breakout"
            and _entry_phase(candidate) in {"RETEST", "ARMED"}
            and bool(policy["trend_aligned"])
            and momentum_confirmations >= max(1, exploration_confirmations_required - 1)
        ) or (
            setup_type == "breakout"
            and _entry_phase(candidate) == "TRIGGERED"
            and bool(policy["trend_aligned"])
            and features["anti_chase"] >= 0.75
            and momentum_confirmations >= exploration_confirmations_required
        )
        exploration_signal_ok = momentum_confirmed or structured_entry_confirmed
        exploration_model_ok = expected >= exploration_expected and lower >= exploration_lower
        exploration_admitted = bool(
            live_enabled
            and exploration_enabled
            and policy["exploration_scope"]
            and rank >= exploration_rank
            and model["quality"] >= exploration_quality
            and exploration_signal_ok
            and exploration_model_ok
            and model["cost_ratio"] >= exploration_cost_ratio
            and executable
            and exploration_liquidity["passed"]
            and not negative_evidence
        )
        v44_scope = bool(policy["live_scope"] or policy["canary_scope"] or policy["exploration_scope"])
        effective_v44_rank = v44_rank
        effective_v44_expected = v44_expected
        effective_v44_confirmations = v44_confirmations_required
        if version.lower().startswith("v4.9"):
            effective_v44_rank = _clamp(
                v44_rank + float(calibration.get("rank_threshold_delta") or 0.0),
                float(config.get("opportunity_v49_global_min_rank_percentile", 0.65)),
                float(config.get("opportunity_v49_global_max_rank_percentile", 0.90)),
            )
            effective_v44_expected = _clamp(
                v44_expected + float(calibration.get("expectancy_threshold_delta_pct") or 0.0),
                float(config.get("opportunity_v49_global_min_expectancy_pct", 0.0)),
                float(config.get("opportunity_v49_global_max_expectancy_pct", 0.20)),
            )
            effective_v44_confirmations = int(_clamp(
                v44_confirmations_required + int(calibration.get("confirmation_delta") or 0),
                float(config.get("opportunity_v49_global_min_confirmations", 3)),
                float(config.get("opportunity_v49_global_max_confirmations", 5)),
            ))
        effective_absolute_quality = (
            v44_quality
            + float(evidence_policy.get("threshold_delta") or 0.0)
            + float(calibration.get("quality_threshold_delta") or 0.0)
        )
        effective_v44_cost_ratio = v44_cost_ratio + float(calibration.get("cost_ratio_delta") or 0.0)
        v44_admitted = bool(
            v44_active
            and live_enabled
            and v44_scope
            and setup_type != "unknown"
            and rank >= effective_v44_rank
            and model["quality"] >= effective_absolute_quality
            and expected >= effective_v44_expected
            and lower >= v44_lower
            and model["cost_ratio"] >= effective_v44_cost_ratio
            and v44_confirmations >= effective_v44_confirmations
            and direction_quality_ok
            and executable
            and full_bet_liquidity["passed"]
            and not bool(exhaustion.get("blocked"))
            and not bool(evidence_policy.get("blocked"))
            and not bool(reentry_policy.get("blocked"))
        )
        permit_eligible = canary_eligible or exploration_admitted
        bootstrap_admitted = canary_eligible
        admitted = validated or provisional or bootstrap_admitted or exploration_admitted
        admission_lane = (
            "validated"
            if validated
            else "core_provisional"
            if provisional
            else "core_canary"
            if bootstrap_admitted
            else "limited_exploration"
            if exploration_admitted
            else "shadow_only"
        )
        lane_risk_multiplier = (
            validated_risk
            if validated
            else provisional_risk
            if provisional
            else bootstrap_risk
            if bootstrap_admitted
            else exploration_risk
            if exploration_admitted
            else 0.0
        )
        risk_multiplier = lane_risk_multiplier * evidence_risk_multiplier
        exploring = bool(policy["exploration_scope"])
        selected_liquidity = (
            validated_liquidity
            if validated
            else provisional_liquidity
            if provisional
            else exploration_liquidity
            if exploring
            else canary_liquidity
        )
        if v44_active:
            validated = False
            provisional = False
            bootstrap_admitted = False
            exploration_admitted = False
            permit_eligible = v44_admitted
            admitted = v44_admitted
            admission_lane = "full_bet" if v44_admitted else "shadow_only"
            lane_risk_multiplier = 1.0 if v44_admitted else 0.0
            risk_multiplier = (
                lane_risk_multiplier
                * adaptive_risk_multiplier
                * float(exhaustion.get("risk_multiplier") or 1.0)
                * float(evidence_policy.get("risk_multiplier") or 1.0)
                * float(reentry_policy.get("risk_multiplier") or 1.0)
            )
            exploring = False
            selected_liquidity = full_bet_liquidity
        blockers: list[str] = []
        applicable_rank = effective_v44_rank if v44_active else exploration_rank if exploring else bootstrap_rank
        applicable_quality = effective_absolute_quality if v44_active else exploration_quality if exploring else bootstrap_quality
        applicable_expected = effective_v44_expected if v44_active else exploration_expected if exploring else min_expected
        applicable_lower = v44_lower if v44_active else exploration_lower if exploring else min_lower
        applicable_cost_ratio = effective_v44_cost_ratio if v44_active else exploration_cost_ratio if exploring else min_cost_ratio
        if not policy["canary_scope"] and not policy["live_scope"] and not policy["exploration_scope"]:
            blockers.append(policy["reason"])
        if rank < applicable_rank:
            blockers.append(f"本轮净期望排名 {rank * 100:.0f}%，未进入前 {100 - applicable_rank * 100:.0f}%")
        if model["quality"] < applicable_quality:
            blockers.append(f"模型质量 {model['quality'] * 100:.1f}，低于当前通道门槛 {applicable_quality * 100:.1f}")
        if expected < applicable_expected:
            blockers.append(f"扣费后融合期望 {expected:.3f}% 低于 {applicable_expected:.3f}%")
        if lower < applicable_lower:
            blockers.append(f"扣费后保守期望 {lower:.3f}% 低于 {applicable_lower:.3f}%")
        if model["cost_ratio"] < applicable_cost_ratio:
            blockers.append(f"毛利/成本比 {model['cost_ratio']:.2f} 低于 {applicable_cost_ratio:.2f}")
        if exploring and not exploration_signal_ok:
            blockers.append(
                f"受限探索确认不足：{momentum_confirmations}/{exploration_confirmations_required}，或回踩/突破结构未确认"
            )
        if v44_active and v44_confirmations < effective_v44_confirmations:
            blockers.append(f"{version.upper()} 五项确认仅通过 {v44_confirmations}/{effective_v44_confirmations}")
        if v44_active and not direction_quality_ok:
            blockers.append(
                "逆市场方向需要更强的资金流、市场匹配和中周期路径确认"
                if version.lower().startswith(("v4.7", "v4.8", "v4.9"))
                else "做空需要更强的方向资金流、市场匹配和中周期路径确认"
            )
        if not v44_active and not exploring and not alignment_ok:
            blockers.append("中周期方向未对齐")
        if negative_evidence and not v44_active:
            circuit_reason = str(local_circuit.get("reason") or "local_shadow_negative")
            blockers.append(
                "同市场/方向/形态/阶段局部熔断：连续实盘净亏损"
                if circuit_reason == "two_consecutive_live_losses"
                else "同市场/方向/形态/阶段局部影子证据为负"
            )
        if v48_active and exhaustion.get("blocked"):
            blockers.append(f"V4.8 趋势衰竭分 {float(exhaustion.get('score') or 0) * 100:.0f}，禁止在末端追价")
        if v48_active and evidence_policy.get("blocked"):
            blockers.append("V4.8 同市场/方向/结构负证据达到硬门，等待新结构")
        if v48_active and reentry_policy.get("blocked"):
            blockers.append("V4.8 同币同方向连续亏损且结构未重置，暂缓重复入场")
        if not executable:
            blockers.append("当前权益下不满足交易所最小下单量")
        if not selected_liquidity["passed"]:
            blockers.append(
                "盘口动态门未通过："
                f"点差 {selected_liquidity['spread_pct']:.4f}%，"
                f"深度 {selected_liquidity['depth_notional']:.0f}/{selected_liquidity['required_depth_notional']:.0f}U，"
                f"预计占盘口 {selected_liquidity['order_book_share_pct']:.2f}%"
            )

        status = (
            "full_bet"
            if v44_admitted
            else "validated"
            if validated
            else "provisional"
            if provisional
            else "canary"
            if bootstrap_admitted
            else "exploration"
            if exploration_admitted
            else "blocked_negative"
            if negative_evidence
            else "collecting"
        )
        version_label = version.upper()
        opportunity = {
            "enabled": True,
            "engine": "opportunity_v4",
            "strategy_family": V4_STRATEGY_FAMILY,
            "strategy_version": version,
            "strategy_role": "active" if live_enabled else "challenger",
            "feature_schema_version": V4_FEATURE_SCHEMA,
            "market_structure_schema": MARKET_STRUCTURE_SCHEMA,
            "setup_type": setup_type,
            "direction": direction,
            "score": round(model["quality"] * 100, 4),
            "base_quality_score": round(model["base_quality"] * 100, 4),
            "setup_adjustment_points": round(model["setup_adjustment"] * 100, 4),
            "smart_flow_adjustment_points": round(float(model.get("smart_flow_adjustment") or 0.0) * 100, 4),
            "counterfactuals": {
                "smart_flow_ablation_score": round(float(model.get("quality_without_smart") or 0.0) * 100, 4),
                "v47_reference_pass": bool(
                    rank >= v44_rank
                    and model["quality"] >= float(config.get("opportunity_v44_min_quality_score", 52.0)) / 100
                    and expected >= float(config.get("opportunity_v44_min_expected_net_pct", 0.02))
                    and lower >= float(config.get("opportunity_v44_min_lower_expectancy_pct", -0.05))
                    and model["cost_ratio"] >= float(config.get("opportunity_v44_min_cost_ratio", 1.50))
                    and v44_confirmations >= v44_confirmations_required
                    and executable
                    and full_bet_liquidity["passed"]
                ),
            },
            "feature_weights": dict(V462_FEATURE_WEIGHTS),
            "rank_percentile": round(rank, 6),
            "rank_bucket": "top" if rank >= 0.75 else "middle" if rank >= 0.35 else "tail",
            "model_expected_net_pct": round(model["expected_net_pct"], 6),
            "expected_net_pct": round(expected, 6),
            "lower_expected_net_pct": round(lower, 6),
            "uncertainty_pct": round(model["uncertainty_pct"] * (1.0 - empirical_weight), 6),
            "model_win_probability": round(model["win_probability"], 6),
            "estimated_cost_pct": round(model["cost_pct"], 6),
            "cost_ratio": round(model["cost_ratio"], 4),
            "features": {key: round(value, 6) for key, value in features.items()},
            "evidence": evidence,
            "local_circuit": local_circuit,
            "evidence_policy": evidence_policy,
            "exhaustion": exhaustion,
            "reentry_policy": reentry_policy,
            "evidence_risk_multiplier": round(evidence_risk_multiplier, 4),
            "evidence_status": status,
            "regime_policy": policy,
            "protection_profile": _protection_profile(candidate, config),
            "shadow_eligible": shadow_eligible,
            "validated": validated,
            "provisional": provisional,
            "canary_eligible": permit_eligible,
            "core_canary_eligible": canary_eligible,
            "bootstrap_admitted": bootstrap_admitted,
            "exploration_admitted": exploration_admitted,
            "full_bet_admitted": v44_admitted,
            "admission_lane": admission_lane,
            "momentum_confirmations": momentum_confirmations,
            "momentum_confirmations_required": exploration_confirmations_required,
            "v44_confirmations": v44_confirmations,
            "v44_confirmations_required": effective_v44_confirmations,
            "adaptive_calibration": calibration,
            "admitted": admitted,
            "passed": admitted,
            "risk_multiplier": round(risk_multiplier, 4),
            "lane_risk_multiplier": round(lane_risk_multiplier, 4),
            "legacy_v3_quality_used_for_live": False,
            "liquidity_gate": selected_liquidity,
            "blockers": blockers,
            "reason": (
                f"{version_label} 局部证据充分，允许标准实盘"
                if validated
                else f"{version_label} 局部证据初步达标，允许受限实盘"
                if provisional
                else f"{version_label} 顺势核心候选可使用限次许可证试单"
                if bootstrap_admitted
                else f"{version_label} 顺势机会通过受限探索通道"
                if exploration_admitted
                else "；".join(blockers or [f"继续积累 {version_label} 事件级独立影子证据"])
            ),
        }
        if v44_active:
            opportunity["reason"] = (
                f"{version.upper()} 相对排名、五项确认、扣费后期望和流动性均达标，允许 S0 全仓短打"
                if v44_admitted
                else "；".join(blockers or [f"继续积累 {version.upper()} 当前版本影子证据"])
            )
        opportunity["position_confidence"] = (
            v44_position_confidence(opportunity, config)
            if v44_active
            else continuous_position_confidence(opportunity, config)
        )
        candidate["opportunity_v4"] = opportunity
        ranked.append((rank, candidate))

    ranked.sort(key=lambda item: (item[1]["opportunity_v4"]["lower_expected_net_pct"], item[0]), reverse=True)
    for index, (_, candidate) in enumerate(ranked):
        candidate["opportunity_v4"]["decision_candidate"] = index < decision_limit
        candidate["opportunity_v4"]["decision_rank"] = index + 1
    return candidates
