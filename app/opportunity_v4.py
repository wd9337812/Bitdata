from __future__ import annotations

import json
import math
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from statistics import mean, pstdev
from typing import Any

from app.telemetry import connect, db_path


V4_STRATEGY_FAMILY = "extreme_v4_roll"
V4_CONTROL_FAMILY = "extreme_v4_control"
V4_FEATURE_SCHEMA = "v4.0"

_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _percentile_rank(values: list[float], value: float) -> float:
    if len(values) <= 1:
        return 1.0
    below = sum(1 for item in values if item < value)
    equal = sum(1 for item in values if item == value)
    return _clamp((below + max(0, equal - 1) * 0.5) / (len(values) - 1), 0.0, 1.0)


def _load_evidence(config: dict[str, Any]) -> list[dict[str, Any]]:
    lookback_hours = float(config.get("opportunity_v4_evidence_lookback_hours", 168))
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat()
    limit = int(config.get("opportunity_v4_evidence_max_trades", 2500))
    with connect() as conn:
        try:
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(shadow_trades)").fetchall()}
            evidence_expr = "COALESCE(evidence_type, 'decision')" if "evidence_type" in columns else "'decision'"
            rows = conn.execute(
                f"SELECT direction, signal_type, market_regime, notional, net_pnl, opportunity_id, "
                f"payload, {evidence_expr} AS evidence_type FROM shadow_trades "
                "WHERE status = 'CLOSED' AND strategy_family = ? AND strategy_version = ? "
                "AND closed_at >= ? ORDER BY id DESC LIMIT ?",
                (
                    V4_STRATEGY_FAMILY,
                    str(config.get("opportunity_v4_strategy_version") or "v4.0-candidate"),
                    cutoff,
                    limit,
                ),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if str(item.get("evidence_type") or "decision") not in {"decision", "exploration"}:
            continue
        try:
            payload = json.loads(item.get("payload") or "{}")
        except json.JSONDecodeError:
            payload = {}
        notional = max(float(item.get("notional") or 0), 0.00000001)
        result.append(
            {
                "direction": str(item.get("direction") or "").upper(),
                "entry_type": str(item.get("signal_type") or "unknown"),
                "market_regime": str(item.get("market_regime") or "unknown"),
                "opportunity_id": str(item.get("opportunity_id") or ""),
                "evidence_type": str(item.get("evidence_type") or "decision"),
                "rank_bucket": str(payload.get("rank_bucket") or "unknown"),
                "net_pct": float(item.get("net_pnl") or 0) / notional * 100,
            }
        )
    return result


def evidence_rows(config: dict[str, Any]) -> list[dict[str, Any]]:
    key = str(db_path())
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
        "win_rate": round(len(wins) / count * 100, 2) if count else 0.0,
        "profit_factor": round(positive / abs(negative), 4) if negative < 0 else (999.0 if positive > 0 else 0.0),
        "net_pct": round(sum(values), 6),
        "expected_net_pct": round(average, 6),
        "lower_expected_net_pct": round(lower, 6),
        "standard_error_pct": round(standard_error, 6),
    }


def _cohort_evidence(candidate: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    direction = str(candidate.get("direction") or "").upper()
    entry_type = str(candidate.get("entry_type") or "unknown")
    regime = str((candidate.get("opportunity_v3") or {}).get("market_regime") or (candidate.get("market_state") or {}).get("state") or "unknown")
    rows = evidence_rows(config)
    exact = [
        row for row in rows
        if row["direction"] == direction and row["entry_type"] == entry_type and row["market_regime"] == regime
    ]
    setup = [row for row in rows if row["direction"] == direction and row["entry_type"] == entry_type]
    direction_rows = [row for row in rows if row["direction"] == direction]
    selected = exact if len(exact) >= 12 else setup if len(setup) >= 20 else direction_rows
    scope = "regime_direction_setup" if selected is exact else "direction_setup" if selected is setup else "direction"
    return {"scope": scope, "selected": _stats(selected), "exact": _stats(exact), "setup": _stats(setup)}


def _model_features(candidate: dict[str, Any]) -> dict[str, float]:
    signal = candidate.get("signal") or {}
    opportunity = candidate.get("opportunity_v3") or {}
    depth = candidate.get("depth") or {}
    strength = float(opportunity.get("strength_percentile") or 0.5)
    direction_fit = _clamp(float(opportunity.get("direction_multiplier") or 0.8) / 1.15, 0.0, 1.0)
    volume = _clamp((float(signal.get("volume_acceleration") or 1.0) - 0.75) / 1.5, 0.0, 1.0)
    flow = _clamp((float(signal.get("directed_trade_flow") or 0.5) - 0.42) / 0.20, 0.0, 1.0)
    path = _clamp(float(opportunity.get("medium_path_efficiency") or 0.0) / 0.35, 0.0, 1.0)
    medium = 1.0 if opportunity.get("medium_trend_aligned") else 0.35 if opportunity.get("medium_ready") else 0.50
    extension = float(signal.get("breakout_extension_atr") or 0.0)
    impulse = max(0.0, float(signal.get("impulse_atr") or 0.0))
    wick = float(signal.get("adverse_wick_ratio") or 0.0)
    anti_chase = 1.0 - _clamp(max(extension / 1.0, impulse / 2.0, wick / 4.0), 0.0, 1.0)
    spread = float(depth.get("spread_pct") or 999.0)
    depth_notional = float(depth.get("depth_notional") or 0.0)
    liquidity = 0.0 if spread >= 999 else 0.55 * _clamp(1.0 - spread / 0.15, 0.0, 1.0) + 0.45 * _clamp(depth_notional / 10_000, 0.0, 1.0)
    return {
        "cross_sectional_strength": strength,
        "regime_fit": direction_fit,
        "volume_persistence": volume,
        "directed_flow": flow,
        "medium_path": path,
        "medium_alignment": medium,
        "anti_chase": anti_chase,
        "liquidity": liquidity,
    }


def _model_expectancy(candidate: dict[str, Any], features: dict[str, float]) -> dict[str, float]:
    quality = (
        features["cross_sectional_strength"] * 0.22
        + features["regime_fit"] * 0.18
        + features["volume_persistence"] * 0.14
        + features["directed_flow"] * 0.12
        + features["medium_path"] * 0.10
        + features["medium_alignment"] * 0.08
        + features["anti_chase"] * 0.10
        + features["liquidity"] * 0.06
    )
    signal = candidate.get("signal") or {}
    entry = max(float(signal.get("last_price") or 0), 0.00000001)
    stop = float(signal.get("stop") or 0)
    stop_pct = abs(entry - stop) / entry * 100 if stop > 0 else 1.0
    reward_pct = float(candidate.get("expected_profit_pct") or signal.get("expected_profit_pct") or 0)
    cost_pct = float(candidate.get("estimated_cost_pct") or 0.12)
    win_probability = _clamp(0.26 + quality * 0.34, 0.30, 0.60)
    expected = win_probability * reward_pct - (1.0 - win_probability) * stop_pct - cost_pct
    uncertainty = 0.22 + (1.0 - quality) * 0.40
    return {
        "quality": quality,
        "win_probability": win_probability,
        "expected_net_pct": expected,
        "lower_expected_net_pct": expected - uncertainty,
        "uncertainty_pct": uncertainty,
        "stop_pct": stop_pct,
        "reward_pct": reward_pct,
        "cost_pct": cost_pct,
    }


def attach_v4_rankings(candidates: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    if not config.get("opportunity_v4_enabled", True):
        return candidates
    prepared: list[tuple[dict[str, Any], dict[str, float], dict[str, float]]] = []
    for candidate in candidates:
        signal = candidate.get("signal") or {}
        direction = str(candidate.get("direction") or "").upper()
        if direction not in {"LONG", "SHORT"} or signal.get("signal") != direction:
            continue
        features = _model_features(candidate)
        model = _model_expectancy(candidate, features)
        prepared.append((candidate, features, model))
    model_values = [item[2]["expected_net_pct"] for item in prepared]
    min_rank = float(config.get("opportunity_v4_decision_min_rank_percentile", 0.75))
    min_samples = int(config.get("opportunity_v4_admission_min_trades", 40))
    min_pf = float(config.get("opportunity_v4_admission_min_profit_factor", 1.10))
    min_lower = float(config.get("opportunity_v4_admission_min_lower_expectancy_pct", 0.02))
    version = str(config.get("opportunity_v4_strategy_version") or "v4.0-candidate")
    decision_limit = int(config.get("opportunity_v4_decision_shadow_limit", 3))
    ranked: list[tuple[float, dict[str, Any]]] = []
    for candidate, features, model in prepared:
        rank = _percentile_rank(model_values, model["expected_net_pct"])
        evidence = _cohort_evidence(candidate, config)
        selected = evidence["selected"]
        empirical_weight = _clamp(float(selected["trades"]) / max(min_samples, 1), 0.0, 1.0)
        expected = model["expected_net_pct"] * (1.0 - empirical_weight) + float(selected["expected_net_pct"]) * empirical_weight
        lower = model["lower_expected_net_pct"] * (1.0 - empirical_weight) + float(selected["lower_expected_net_pct"]) * empirical_weight
        entry_type = str(candidate.get("entry_type") or "unknown")
        execution = candidate.get("execution_filter") or {}
        executable = not execution.get("enabled") or bool(execution.get("executable"))
        # Shadow exploration must not inherit V3's zero-risk sizing or minimum-order rejection.
        # It follows real prices without placing an exchange order, so execution viability is
        # reserved for eventual live admission only.
        shadow_eligible = bool(entry_type.startswith("v3_") and float(model["reward_pct"]) > float(model["cost_pct"]))
        admitted = bool(
            config.get("opportunity_v4_live_enabled", False)
            and entry_type == "v3_breakout"
            and rank >= min_rank
            and selected["trades"] >= min_samples
            and selected["profit_factor"] >= min_pf
            and lower >= min_lower
            and executable
        )
        blockers: list[str] = []
        if entry_type != "v3_breakout":
            blockers.append("首个 V4 实盘版本只允许趋势突破，其他结构继续积累影子样本")
        if rank < min_rank:
            blockers.append(f"本轮净期望排名 {rank * 100:.0f}% 未进入前 {100 - min_rank * 100:.0f}%")
        if selected["trades"] < min_samples:
            blockers.append(f"同类证据 {selected['trades']} / {min_samples} 笔")
        elif selected["profit_factor"] < min_pf or lower < min_lower:
            blockers.append("同类扣费后证据尚未达到准入标准")
        if not executable:
            blockers.append("当前权益下不满足交易所最小下单量")
        opportunity = {
            "enabled": True,
            "engine": "opportunity_v4",
            "strategy_family": V4_STRATEGY_FAMILY,
            "strategy_version": version,
            "strategy_role": "challenger",
            "feature_schema_version": V4_FEATURE_SCHEMA,
            "score": round(model["quality"] * 100, 4),
            "rank_percentile": round(rank, 6),
            "rank_bucket": "top" if rank >= 0.75 else "middle" if rank >= 0.35 else "tail",
            "model_expected_net_pct": round(model["expected_net_pct"], 6),
            "expected_net_pct": round(expected, 6),
            "lower_expected_net_pct": round(lower, 6),
            "uncertainty_pct": round(model["uncertainty_pct"] * (1.0 - empirical_weight), 6),
            "model_win_probability": round(model["win_probability"], 6),
            "features": {key: round(value, 6) for key, value in features.items()},
            "evidence": evidence,
            "evidence_status": "admitted" if admitted else "collecting" if selected["trades"] < min_samples else "rejected",
            "shadow_eligible": shadow_eligible,
            "admitted": admitted,
            "passed": admitted,
            "blockers": blockers,
            "reason": "V4 候选级净期望准入通过" if admitted else "；".join(blockers or ["继续积累公平影子样本"]),
        }
        candidate["opportunity_v4"] = opportunity
        ranked.append((rank, candidate))
    ranked.sort(key=lambda item: (item[1]["opportunity_v4"]["lower_expected_net_pct"], item[0]), reverse=True)
    for index, (_, candidate) in enumerate(ranked):
        candidate["opportunity_v4"]["decision_candidate"] = index < decision_limit
        candidate["opportunity_v4"]["decision_rank"] = index + 1
    return candidates
