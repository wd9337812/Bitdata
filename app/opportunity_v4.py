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
V4_FEATURE_SCHEMA = "v4.1"

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
    opportunity = candidate.get("opportunity_v3") or {}
    state = candidate.get("market_state") or {}
    return str(opportunity.get("market_regime") or state.get("state") or "unknown").lower()


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
                    str(config.get("opportunity_v4_strategy_version") or "v4.1"),
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
                "entry_type": str(item.get("signal_type") or "unknown"),
                "market_regime": str(item.get("market_regime") or "unknown").lower(),
                "entry_phase": str(payload_features.get("entry_phase") or payload.get("entry_phase") or "UNKNOWN").upper(),
                "medium_trend_aligned": bool(payload_features.get("medium_trend_aligned", payload.get("medium_trend_aligned"))),
                "opportunity_id": opportunity_id,
                "rank_bucket": str(payload.get("rank_bucket") or "unknown"),
                "cost_pct": abs(float(item.get("estimated_cost") or 0)) / notional * 100,
                "net_pct": float(item.get("net_pnl") or 0) / notional * 100,
            }
        )
    return result


def evidence_rows(config: dict[str, Any]) -> list[dict[str, Any]]:
    key = f"{db_path()}:{config.get('opportunity_v4_strategy_version', 'v4.1')}"
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


def _cohort_evidence(candidate: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    direction = str(candidate.get("direction") or "").upper()
    entry_type = str(candidate.get("entry_type") or "unknown")
    regime = _market_regime(candidate)
    phase = _entry_phase(candidate)
    rows = evidence_rows(config)
    exact_phase = [
        row
        for row in rows
        if row["direction"] == direction
        and row["entry_type"] == entry_type
        and row["market_regime"] == regime
        and row["entry_phase"] == phase
    ]
    exact = [
        row
        for row in rows
        if row["direction"] == direction and row["entry_type"] == entry_type and row["market_regime"] == regime
    ]
    setup = [row for row in rows if row["direction"] == direction and row["entry_type"] == entry_type]
    direction_rows = [row for row in rows if row["direction"] == direction]
    if exact_phase:
        selected, scope = exact_phase, "regime_direction_setup_phase"
    elif exact:
        selected, scope = exact, "regime_direction_setup"
    elif setup:
        selected, scope = setup, "direction_setup"
    else:
        selected, scope = direction_rows, "direction"
    return {
        "scope": scope,
        "selected": _stats(selected),
        "exact_phase": _stats(exact_phase),
        "exact": _stats(exact),
        "setup": _stats(setup),
        "direction": _stats(direction_rows),
    }


def _model_features(candidate: dict[str, Any]) -> dict[str, float]:
    signal = candidate.get("signal") or {}
    opportunity = candidate.get("opportunity_v3") or {}
    depth = candidate.get("depth") or {}
    phase = _entry_phase(candidate)
    strength = float(opportunity.get("strength_percentile") or 0.5)
    direction_fit = _clamp(float(opportunity.get("direction_multiplier") or 0.8) / 1.15, 0.0, 1.0)
    volume = _clamp((float(signal.get("volume_acceleration") or 1.0) - 0.75) / 1.5, 0.0, 1.0)
    flow = _clamp((float(signal.get("directed_trade_flow") or 0.5) - 0.42) / 0.20, 0.0, 1.0)
    path = _clamp(float(opportunity.get("medium_path_efficiency") or 0.0) / 0.35, 0.0, 1.0)
    medium = 1.0 if opportunity.get("medium_trend_aligned") else 0.35 if opportunity.get("medium_ready") else 0.15
    entry_quality = {"RETEST": 1.0, "ARMED": 0.68, "TRIGGERED": 0.30}.get(phase, 0.45)
    extension = float(signal.get("breakout_extension_atr") or 0.0)
    impulse = max(0.0, float(signal.get("impulse_atr") or 0.0))
    wick = float(signal.get("adverse_wick_ratio") or 0.0)
    anti_chase = 1.0 - _clamp(max(extension / 1.0, impulse / 2.0, wick / 4.0), 0.0, 1.0)
    spread_raw = depth.get("spread_pct")
    spread = float(spread_raw) if spread_raw is not None else 999.0
    depth_notional = float(depth.get("depth_notional") or 0.0)
    liquidity = 0.0 if spread >= 999 else 0.55 * _clamp(1.0 - spread / 0.15, 0.0, 1.0) + 0.45 * _clamp(depth_notional / 10_000, 0.0, 1.0)
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
    }


def _dynamic_cost_pct(candidate: dict[str, Any], config: dict[str, Any]) -> float:
    depth = candidate.get("depth") or {}
    fee_pct = float(config.get("taker_fee_pct_round_trip", 0.08))
    slippage_pct = float(candidate.get("estimated_slippage_pct") or config.get("estimated_slippage_pct", 0.04))
    spread_pct = max(0.0, float(depth.get("spread_pct") or 0.0))
    buffer_pct = float(config.get("opportunity_v41_cost_safety_buffer_pct", 0.02))
    measured = float(candidate.get("estimated_cost_pct") or 0.0)
    return max(measured, fee_pct + slippage_pct + spread_pct + buffer_pct)


def _model_expectancy(candidate: dict[str, Any], features: dict[str, float], config: dict[str, Any]) -> dict[str, float]:
    quality = (
        features["cross_sectional_strength"] * 0.14
        + features["regime_fit"] * 0.14
        + features["volume_persistence"] * 0.10
        + features["directed_flow"] * 0.10
        + features["medium_path"] * 0.12
        + features["medium_alignment"] * 0.18
        + features["entry_quality"] * 0.14
        + features["anti_chase"] * 0.05
        + features["liquidity"] * 0.03
    )
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
        "win_probability": win_probability,
        "expected_net_pct": expected,
        "lower_expected_net_pct": expected - uncertainty,
        "uncertainty_pct": uncertainty,
        "stop_pct": stop_pct,
        "reward_pct": reward_pct,
        "cost_pct": cost_pct,
        "cost_ratio": reward_pct / cost_pct if cost_pct > 0 else 999.0,
    }


def _regime_policy(candidate: dict[str, Any]) -> dict[str, Any]:
    regime = _market_regime(candidate)
    direction = str(candidate.get("direction") or "").upper()
    entry_type = str(candidate.get("entry_type") or "unknown")
    opportunity = candidate.get("opportunity_v3") or {}
    aligned = bool(opportunity.get("medium_trend_aligned"))
    quiet_short = regime == "quiet" and direction == "SHORT" and entry_type in {
        "v3_breakout",
        "v3_pullback",
        "v3_prebreakout",
    }
    mixed_long_pullback = regime == "mixed" and direction == "LONG" and entry_type == "v3_pullback"
    broad_up_long = regime == "broad_up" and direction == "LONG" and entry_type in {"v3_pullback", "v3_breakout"}
    if quiet_short:
        return {"scope": "quiet_short", "live_scope": True, "canary_scope": True, "reason": "静市做空结构有正向历史证据"}
    if aligned and (mixed_long_pullback or broad_up_long):
        return {"scope": "aligned_long_pullback", "live_scope": False, "canary_scope": True, "reason": "顺势做多回踩仅允许新策略试运行"}
    if regime in {"broad_down", "panic", "rotation"}:
        return {"scope": "blocked_regime", "live_scope": False, "canary_scope": False, "reason": "当前市场状态的历史扣费后证据不足"}
    return {"scope": "shadow_only", "live_scope": False, "canary_scope": False, "reason": "该状态组合继续积累独立决策影子"}


def _protection_profile(candidate: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    phase = _entry_phase(candidate)
    prefix = "retest" if phase == "RETEST" else "armed" if phase == "ARMED" else "triggered"
    return {
        "entry_phase": phase,
        "stop_atr": float(config.get(f"opportunity_v41_{prefix}_stop_atr", 0.70)),
        "take_profit_atr": float(config.get(f"opportunity_v41_{prefix}_take_profit_atr", 1.20)),
        "max_hold_bars": int(config.get(f"opportunity_v41_{prefix}_max_hold_bars", 8)),
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
    version = str(config.get("opportunity_v4_strategy_version") or "v4.1")
    decision_limit = int(config.get("opportunity_v4_decision_shadow_limit", 3))
    bootstrap_rank = float(config.get("opportunity_v4_bootstrap_min_rank_percentile", 0.85))
    bootstrap_quality = float(config.get("opportunity_v4_bootstrap_min_quality_score", 58.0)) / 100
    min_expected = max(
        float(config.get("opportunity_v41_min_expected_net_pct", 0.10)),
        float(config.get("opportunity_v4_bootstrap_min_model_expectancy_pct", 0.10)),
    )
    min_cost_ratio = float(config.get("opportunity_v41_min_cost_ratio", 2.0))
    negative_min_trades = int(config.get("opportunity_v4_bootstrap_negative_min_trades", 20))
    negative_pf = float(config.get("opportunity_v4_bootstrap_negative_profit_factor", 0.75))
    require_alignment = bool(config.get("opportunity_v41_medium_alignment_required", True))
    live_enabled = bool(config.get("opportunity_v4_live_enabled", False))
    ranked: list[tuple[float, dict[str, Any]]] = []

    for candidate, features, model in prepared:
        rank = _percentile_rank(model_values, model["expected_net_pct"])
        evidence = _cohort_evidence(candidate, config)
        selected = evidence["selected"]
        empirical_weight = float(selected["trades"]) / (float(selected["trades"]) + max(prior_trades, 1.0))
        expected = model["expected_net_pct"] * (1.0 - empirical_weight) + float(selected["expected_net_pct"]) * empirical_weight
        lower = model["lower_expected_net_pct"] * (1.0 - empirical_weight) + float(selected["lower_expected_net_pct"]) * empirical_weight
        execution = candidate.get("execution_filter") or {}
        executable = not execution.get("enabled") or bool(execution.get("executable"))
        depth = candidate.get("depth") or {}
        spread_raw = depth.get("spread_pct")
        spread_pct = float(spread_raw) if spread_raw is not None else 999.0
        depth_notional = float(depth.get("depth_notional") or 0.0)
        liquidity_ok = bool(
            spread_pct <= float(config.get("opportunity_v3_max_spread_pct", 0.12))
            and depth_notional >= float(config.get("opportunity_v3_min_depth_notional_usdt", 1_000.0))
        )
        policy = _regime_policy(candidate)
        medium_aligned = bool((candidate.get("opportunity_v3") or {}).get("medium_trend_aligned"))
        alignment_ok = medium_aligned or not require_alignment
        cost_ok = model["cost_ratio"] >= min_cost_ratio
        model_ok = model["expected_net_pct"] >= min_expected and model["lower_expected_net_pct"] > min_lower
        shadow_eligible = bool(str(candidate.get("entry_type") or "").startswith("v3_") and model["reward_pct"] > model["cost_pct"])
        negative_evidence = bool(
            selected["trades"] >= negative_min_trades
            and selected["net_pct"] < 0
            and selected["profit_factor"] < negative_pf
        )
        common_gates = executable and liquidity_ok and alignment_ok and cost_ok and model_ok and not negative_evidence
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
            and not validated
        )
        canary_eligible = bool(
            live_enabled
            and config.get("opportunity_v4_bootstrap_enabled", True)
            and policy["canary_scope"]
            and rank >= bootstrap_rank
            and model["quality"] >= bootstrap_quality
            and common_gates
            and not validated
            and not provisional
        )
        bootstrap_admitted = canary_eligible
        admitted = validated or provisional or bootstrap_admitted
        risk_multiplier = (
            float(config.get("opportunity_v4_validated_risk_multiplier", 1.0))
            if validated
            else float(config.get("opportunity_v41_provisional_risk_multiplier", 0.70))
            if provisional
            else float(config.get("opportunity_v4_bootstrap_risk_multiplier", 0.40))
            if bootstrap_admitted
            else 0.0
        )
        blockers: list[str] = []
        if not policy["canary_scope"] and not policy["live_scope"]:
            blockers.append(policy["reason"])
        if rank < bootstrap_rank:
            blockers.append(f"本轮净期望排名 {rank * 100:.0f}%，未进入前 {100 - bootstrap_rank * 100:.0f}%")
        if model["quality"] < bootstrap_quality:
            blockers.append(f"模型质量 {model['quality'] * 100:.1f}，低于探索门槛 {bootstrap_quality * 100:.1f}")
        if model["expected_net_pct"] < min_expected:
            blockers.append(f"扣费后模型期望 {model['expected_net_pct']:.3f}% 低于 {min_expected:.3f}%")
        if model["lower_expected_net_pct"] <= min_lower:
            blockers.append("扣费后保守期望仍未大于零")
        if not cost_ok:
            blockers.append(f"毛利/成本比 {model['cost_ratio']:.2f} 低于 {min_cost_ratio:.2f}")
        if not alignment_ok:
            blockers.append("中周期方向未对齐")
        if negative_evidence:
            blockers.append(f"同状态独立决策证据转负：{selected['trades']} 笔，PF {selected['profit_factor']:.2f}")
        if not executable:
            blockers.append("当前权益下不满足交易所最小下单量")
        if not liquidity_ok:
            blockers.append(f"盘口硬门未通过：点差 {spread_pct:.4f}%，深度 {depth_notional:.0f}U")

        status = "validated" if validated else "provisional" if provisional else "canary" if bootstrap_admitted else "blocked_negative" if negative_evidence else "collecting"
        opportunity = {
            "enabled": True,
            "engine": "opportunity_v4",
            "strategy_family": V4_STRATEGY_FAMILY,
            "strategy_version": version,
            "strategy_role": "active" if live_enabled else "challenger",
            "feature_schema_version": V4_FEATURE_SCHEMA,
            "score": round(model["quality"] * 100, 4),
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
            "evidence_status": status,
            "regime_policy": policy,
            "protection_profile": _protection_profile(candidate, config),
            "shadow_eligible": shadow_eligible,
            "validated": validated,
            "provisional": provisional,
            "canary_eligible": canary_eligible,
            "bootstrap_admitted": bootstrap_admitted,
            "admitted": admitted,
            "passed": admitted,
            "risk_multiplier": round(risk_multiplier, 4),
            "legacy_v3_quality_used_for_live": False,
            "liquidity_gate": {
                "passed": liquidity_ok,
                "spread_pct": round(spread_pct, 6),
                "depth_notional": round(depth_notional, 4),
            },
            "blockers": blockers,
            "reason": (
                "V4.1 状态证据充分，允许标准实盘"
                if validated
                else "V4.1 状态证据初步达标，允许受限实盘"
                if provisional
                else "V4.1 新策略候选可使用限次许可证试单"
                if bootstrap_admitted
                else "；".join(blockers or ["继续积累 V4.1 独立决策影子"])
            ),
        }
        candidate["opportunity_v4"] = opportunity
        ranked.append((rank, candidate))

    ranked.sort(key=lambda item: (item[1]["opportunity_v4"]["lower_expected_net_pct"], item[0]), reverse=True)
    for index, (_, candidate) in enumerate(ranked):
        candidate["opportunity_v4"]["decision_candidate"] = index < decision_limit
        candidate["opportunity_v4"]["decision_rank"] = index + 1
    return candidates
