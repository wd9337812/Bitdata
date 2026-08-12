from __future__ import annotations

from typing import Any

from app.s0_full_bet import is_s0_full_bet
from app.strategy_capabilities import strategy_supports

from app.performance_guard import observed_round_trip_cost_pct


PROBE_ENTRY_TYPES = {
    "extreme_probe",
    "weak_quality_probe",
    "preemptive",
    "momentum",
    "small_standard",
    "observe_standard",
    "orderbook_impact",
    "volume_scalp",
    "imbalance_probe",
    "v3_prebreakout",
}
YOLO_SCALP_ENTRY_TYPES = {
    "standard",
    "extreme_scalp",
    "preemptive",
    "momentum",
    "extreme_probe",
    "weak_quality_probe",
    "observe_standard",
    "small_standard",
    "orderbook_impact",
    "volume_scalp",
    "imbalance_probe",
}


ORDER_VIABILITY_LABELS = {
    "below_effective_min_notional": "低于系统有效下单额",
    "insufficient_profit_cost_ratio": "预期收益/手续费滑点成本比不足",
    "insufficient_expected_net_profit": "扣费后预期净利润不足",
}


def signal_strength_tier(candidate: dict[str, Any] | None) -> str:
    candidate = candidate or {}
    if str(candidate.get("strategy_family") or "") == "extreme_v3_roll":
        return {"A+": "top", "A": "high", "B": "probe"}.get(str(candidate.get("v3_tier") or ""), "probe")
    entry_type = str(candidate.get("entry_type") or "standard")
    score = float(candidate.get("score") or 0.0)
    quality_score = float((candidate.get("symbol_quality") or {}).get("score") or 0.0)
    if entry_type not in PROBE_ENTRY_TYPES and score >= 135 and quality_score >= 75:
        return "top"
    if entry_type not in PROBE_ENTRY_TYPES and score >= 110 and quality_score >= 68:
        return "high"
    if entry_type not in PROBE_ENTRY_TYPES:
        return "standard"
    return "probe"


def extreme_scalp_tier(candidate: dict[str, Any] | None, config: dict[str, Any]) -> str:
    if not config.get("extreme_scalp_enabled", True):
        return "none"
    candidate = candidate or {}
    if str(candidate.get("mode") or "") not in {"extreme_sprint", "yolo_scalp"}:
        return "none"
    if str(candidate.get("entry_type") or "standard") in PROBE_ENTRY_TYPES:
        return "none"
    score = float(candidate.get("score") or 0.0)
    quality_score = float((candidate.get("symbol_quality") or {}).get("score") or 0.0)
    cost_ratio = float(candidate.get("cost_ratio") or 0.0)
    depth_notional = float(((candidate.get("depth") or {}).get("depth_notional")) or 0.0)
    prefix = "yolo_scalp" if str(candidate.get("mode") or "") == "yolo_scalp" else "extreme_scalp"
    min_quality = float(config.get(f"{prefix}_min_quality_score", config.get("extreme_scalp_min_quality_score", 72.0)))
    min_cost_ratio = float(config.get(f"{prefix}_min_cost_ratio", config.get("extreme_scalp_min_cost_ratio", 12.0)))
    min_depth = float(config.get(f"{prefix}_min_depth_notional_usdt", config.get("extreme_scalp_min_depth_notional_usdt", 20_000.0)))
    if quality_score < min_quality or cost_ratio < min_cost_ratio or depth_notional < min_depth:
        return "none"
    if score >= float(config.get(f"{prefix}_super_score", config.get("extreme_scalp_super_score", 145.0))):
        return "super"
    if score >= float(config.get(f"{prefix}_high_score", config.get("extreme_scalp_high_score", 122.0))):
        return "high"
    return "none"


def yolo_scalp_execution_profile(candidate: dict[str, Any] | None, config: dict[str, Any]) -> dict[str, Any]:
    candidate = candidate or {}
    if str(candidate.get("mode") or "") != "yolo_scalp":
        return {"enabled": False, "tier": "none", "label": "非极限梭哈"}
    entry_type = str(candidate.get("entry_type") or "standard")
    score = float(candidate.get("score") or 0.0)
    quality_score = float((candidate.get("symbol_quality") or {}).get("score") or 0.0)
    cost_ratio = float(candidate.get("cost_ratio") or 0.0)
    live_credit = candidate.get("live_credit") or {}
    credit_adjustment = candidate.get("live_credit_adjustment") or {}
    has_loss_pressure = (
        int(live_credit.get("losses") or 0) > 0
        or int(live_credit.get("consecutive_losses") or 0) > 0
        or bool((credit_adjustment.get("cooldown") or {}).get("active"))
        or str(live_credit.get("status") or "") in {"weak", "penalty"}
    )

    if entry_type == "orderbook_impact":
        tier = "orderbook_impact"
        label = "盘口冲击"
        min_key = "yolo_scalp_orderbook_impact_min_risk_pct"
        max_key = "yolo_scalp_orderbook_impact_max_risk_pct"
    elif entry_type == "volume_scalp":
        tier = "volume_scalp"
        label = "放量剥头皮"
        min_key = "yolo_scalp_orderbook_volume_min_risk_pct"
        max_key = "yolo_scalp_orderbook_volume_max_risk_pct"
    elif entry_type == "imbalance_probe":
        tier = "imbalance_probe"
        label = "失衡试探"
        min_key = "yolo_scalp_orderbook_probe_min_risk_pct"
        max_key = "yolo_scalp_orderbook_probe_max_risk_pct"
    elif entry_type == "weak_quality_probe":
        tier = "weak_probe"
        label = "小单探路"
        min_key = "yolo_scalp_weak_probe_min_risk_pct"
        max_key = "yolo_scalp_weak_probe_max_risk_pct"
    elif entry_type == "extreme_probe":
        tier = "firecracker"
        label = "火药桶剥头皮"
        min_key = "yolo_scalp_firecracker_min_risk_pct"
        max_key = "yolo_scalp_firecracker_max_risk_pct"
    elif entry_type in {"preemptive", "momentum", "observe_standard", "small_standard"}:
        tier = "preemptive"
        label = "抢跑剥头皮"
        min_key = "yolo_scalp_preemptive_min_risk_pct"
        max_key = "yolo_scalp_preemptive_max_risk_pct"
    else:
        tier = "standard"
        label = "标准剥头皮"
        min_key = "yolo_scalp_standard_min_risk_pct"
        max_key = "yolo_scalp_standard_max_risk_pct"

    min_risk = float(config.get(min_key, 0.0))
    max_risk = float(config.get(max_key, min_risk))
    multiplier = 1.0
    if score >= float(config.get("yolo_scalp_super_score", 118.0)) and cost_ratio >= float(config.get("yolo_scalp_min_cost_ratio", 4.0)):
        multiplier = float(config.get("yolo_scalp_super_risk_multiplier", 1.8))
    elif score >= float(config.get("yolo_scalp_high_score", 95.0)) and cost_ratio >= float(config.get("yolo_scalp_min_cost_ratio", 4.0)):
        multiplier = float(config.get("yolo_scalp_high_risk_multiplier", 1.35))
    if quality_score < float(config.get("yolo_scalp_min_quality_score", 58.0)) and tier != "weak_probe":
        multiplier *= 0.75
    if has_loss_pressure:
        multiplier *= float(config.get("yolo_scalp_loss_probe_risk_multiplier", 0.75))
        max_risk = min(max_risk, float(config.get("yolo_scalp_loss_probe_max_risk_pct", 12.0)))
    return {
        "enabled": entry_type in YOLO_SCALP_ENTRY_TYPES,
        "tier": tier,
        "label": label,
        "entry_type": entry_type,
        "min_risk_pct": round(min_risk, 6),
        "max_risk_pct": round(max_risk, 6),
        "risk_multiplier": round(multiplier, 6),
        "loss_pressure": has_loss_pressure,
    }


def effective_position_risk(
    *,
    candidate_risk_pct: float,
    candidate: dict[str, Any] | None,
    guard: dict[str, Any] | None,
    target: dict[str, Any] | None,
    config: dict[str, Any],
    mode: str,
) -> dict[str, Any]:
    """Apply drawdown protection once, while keeping qualified orders economically meaningful."""
    candidate = candidate or {}
    guard = guard or {}
    target = target or {}
    raw_risk = max(0.0, float(candidate_risk_pct))
    v4 = candidate.get("opportunity_v4") or {}
    full_bet_applied = is_s0_full_bet(candidate, config)
    position_confidence = v4.get("position_confidence") or {}
    continuous_target = position_confidence.get("target_initial_risk_pct")
    release_fallback_active = bool(config.get("_release_fallback_active")) and not full_bet_applied
    continuous_applied = bool(
        position_confidence.get("applied")
        and continuous_target is not None
        and not release_fallback_active
    )
    sizing_input_risk = max(0.0, float(continuous_target)) if continuous_applied else raw_risk
    tier = signal_strength_tier(candidate)
    guard_multiplier = max(0.0, float(guard.get("risk_multiplier", 1.0)))
    target_multiplier = max(0.0, float(target.get("effective_risk_multiplier", 1.0)))
    guard_floor = 0.0
    risk_floor = 0.0
    if (
        config.get("effective_position_sizing_enabled", True)
        and mode in {"extreme_sprint", "yolo_scalp"}
        and not full_bet_applied
    ):
        guard_floor = float(config.get(f"effective_guard_{tier}_min_multiplier", 0.2))
        guard_multiplier = max(guard_multiplier, guard_floor)
        risk_floor = float(config.get(f"effective_{tier}_min_risk_pct", 0.0))
    calculated = sizing_input_risk * guard_multiplier * target_multiplier
    yolo_profile = yolo_scalp_execution_profile(candidate, config) if mode == "yolo_scalp" else {"enabled": False}
    scalp_tier = (
        extreme_scalp_tier(candidate, config)
        if mode in {"extreme_sprint", "yolo_scalp"} and not full_bet_applied
        else "none"
    )
    scalp_multiplier = 1.0
    if scalp_tier == "high":
        key_prefix = "yolo_scalp" if mode == "yolo_scalp" else "extreme_scalp"
        scalp_multiplier = float(config.get(f"{key_prefix}_high_risk_multiplier", config.get("extreme_scalp_high_risk_multiplier", 1.25)))
    elif scalp_tier == "super":
        key_prefix = "yolo_scalp" if mode == "yolo_scalp" else "extreme_scalp"
        scalp_multiplier = float(config.get(f"{key_prefix}_super_risk_multiplier", config.get("extreme_scalp_super_risk_multiplier", 1.55)))
    if yolo_profile.get("enabled"):
        scalp_tier = str(yolo_profile.get("tier") or scalp_tier)
        scalp_multiplier *= float(yolo_profile.get("risk_multiplier") or 1.0)
    calculated *= scalp_multiplier
    final_risk = max(calculated, risk_floor) if sizing_input_risk > 0 else 0.0
    if yolo_profile.get("enabled") and sizing_input_risk > 0:
        final_risk = max(final_risk, float(yolo_profile.get("min_risk_pct") or 0.0))
    max_risk = float(config.get(f"effective_{tier}_max_risk_pct", sizing_input_risk or final_risk))
    if continuous_applied:
        # V4.3.2 uses its own continuous range rather than the legacy score-tier
        # caps. The stage route remains the final hard ceiling below.
        max_risk = max(max_risk, float(config.get("opportunity_v432_initial_max_risk_pct", 7.5)))
    if full_bet_applied:
        version = str(config.get("opportunity_v4_strategy_version") or "")
        profile_cap = (
            float(config.get("opportunity_v50_max_risk_pct", 30.0))
            if strategy_supports(version, "v50_s30")
            else float(config.get("opportunity_v44_max_risk_pct", 15.0))
        )
        max_risk = max(max_risk, profile_cap)
    if scalp_tier != "none":
        key_prefix = "yolo_scalp" if mode == "yolo_scalp" else "extreme_scalp"
        scalp_cap = float(config.get(f"{key_prefix}_max_risk_pct", config.get("extreme_scalp_max_risk_pct", 18.0)))
        max_risk = scalp_cap if scalp_cap > 0 else max_risk
    if yolo_profile.get("enabled"):
        max_risk = float(yolo_profile.get("max_risk_pct") or max_risk)
    stage_route = config.get("_stage_route") or {}
    stage_risk_cap = float(stage_route.get("risk_pct") or 0.0)
    version = str(config.get("opportunity_v4_strategy_version") or "")
    if stage_risk_cap > 0 and not strategy_supports(version, "v50_s30"):
        max_risk = min(max_risk, stage_risk_cap) if max_risk > 0 else stage_risk_cap
    if max_risk > 0:
        final_risk = min(final_risk, max_risk)
    performance = candidate.get("global_performance_guard") or {}
    performance_multiplier = max(0.0, min(1.0, float(performance.get("risk_multiplier", 1.0))))
    performance_cap = sizing_input_risk * performance_multiplier
    performance_mode = "multiplier"
    if str(performance.get("status") or "").startswith(("recovery_", "strategy_canary_")):
        # Recovery and drawdown protection are independent absolute caps. Multiplying
        # both can make a valid probe economically meaningless.
        final_risk = min(final_risk, performance_cap)
        performance_mode = "minimum_cap"
    else:
        final_risk *= performance_multiplier
    absolute_performance_cap = performance.get("risk_cap_pct")
    performance_absolute_cap = (
        max(0.0, float(absolute_performance_cap))
        if absolute_performance_cap is not None
        else None
    )
    if performance_absolute_cap is not None:
        final_risk = min(final_risk, performance_absolute_cap)
        performance_mode = "absolute_cap"
    return {
        "tier": tier,
        "scalp_tier": scalp_tier,
        "scalp_multiplier": round(scalp_multiplier, 6),
        "candidate_risk_pct": round(raw_risk, 8),
        "continuous_quality_applied": continuous_applied,
        "full_bet_applied": full_bet_applied,
        "release_fallback_active": release_fallback_active,
        "continuous_confidence": round(float(position_confidence.get("confidence") or 0.0), 6),
        "continuous_target_risk_pct": round(sizing_input_risk, 8),
        "continuous_display_label": position_confidence.get("display_label"),
        "guard_multiplier": round(guard_multiplier, 6),
        "guard_floor": round(guard_floor, 6),
        "target_multiplier": round(target_multiplier, 6),
        "risk_floor_pct": round(risk_floor, 6),
        "max_risk_pct": round(max_risk, 6),
        "stage_risk_cap_pct": round(stage_risk_cap, 6),
        "performance_multiplier": round(performance_multiplier, 6),
        "performance_cap_pct": round(performance_cap, 6),
        "performance_absolute_cap_pct": (
            round(performance_absolute_cap, 6)
            if performance_absolute_cap is not None
            else None
        ),
        "performance_mode": performance_mode,
        "final_risk_pct": round(final_risk, 8),
        "risk_chain": {
            "stage_risk_pct": round(float((config.get("_stage_route") or {}).get("risk_pct") or raw_risk), 8),
            "channel_risk_pct": round(raw_risk, 8),
            "continuous_quality_risk_pct": round(sizing_input_risk, 8),
            "guard_and_target_risk_pct": round(calculated, 8),
            "license_or_performance_cap_pct": round(performance_cap, 8),
            "absolute_performance_cap_pct": (
                round(performance_absolute_cap, 8)
                if performance_absolute_cap is not None
                else None
            ),
            "final_risk_pct": round(final_risk, 8),
        },
        "yolo_scalp_profile": yolo_profile,
    }


def effective_order_viability(
    *,
    notional: float,
    candidate: dict[str, Any] | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    candidate = candidate or {}
    expected_profit_pct = max(0.0, float(candidate.get("expected_profit_pct") or 0.0))
    candidate_cost_pct = max(0.0, float(candidate.get("estimated_cost_pct") or 0.0))
    observed_cost_pct = observed_round_trip_cost_pct(config)
    estimated_cost_pct = max(candidate_cost_pct, observed_cost_pct)
    cost_ratio = expected_profit_pct / estimated_cost_pct if estimated_cost_pct else 999.0
    expected_net_profit = float(notional) * max(0.0, expected_profit_pct - estimated_cost_pct) / 100
    is_yolo = str(candidate.get("mode") or "") == "yolo_scalp"
    min_notional = float(
        config.get("yolo_scalp_effective_min_order_notional_usdt", 5.0)
        if is_yolo
        else config.get("effective_min_order_notional_usdt", 10.0)
    )
    min_cost_ratio = float(
        config.get("yolo_scalp_min_order_lift_min_cost_ratio", 3.0)
        if is_yolo
        else config.get("effective_min_profit_cost_ratio", 3.0)
    )
    min_net_profit = float(
        config.get("yolo_scalp_min_order_lift_min_net_profit_usdt", 0.03)
        if is_yolo
        else config.get("effective_min_net_profit_usdt", 0.15)
    )
    reasons = []
    if float(notional) < min_notional:
        reasons.append("below_effective_min_notional")
    if cost_ratio < min_cost_ratio:
        reasons.append("insufficient_profit_cost_ratio")
    if expected_net_profit < min_net_profit:
        reasons.append("insufficient_expected_net_profit")
    reason_details = {
        "below_effective_min_notional": (
            f"当前名义金额 {float(notional):.4f}U，低于最低有效下单额 {min_notional:.4f}U"
        ),
        "insufficient_profit_cost_ratio": (
            f"收益/成本比 {cost_ratio:.2f}，低于最低 {min_cost_ratio:.2f}"
        ),
        "insufficient_expected_net_profit": (
            f"扣费后预期净利润 {expected_net_profit:.4f}U，低于最低 {min_net_profit:.4f}U"
        ),
    }
    return {
        "allowed": not reasons,
        "notional": round(float(notional), 8),
        "minimum_notional": min_notional,
        "cost_ratio": round(cost_ratio, 6),
        "candidate_cost_pct": round(candidate_cost_pct, 6),
        "observed_cost_floor_pct": round(observed_cost_pct, 6),
        "minimum_cost_ratio": min_cost_ratio,
        "expected_net_profit": round(expected_net_profit, 8),
        "minimum_net_profit": min_net_profit,
        "reasons": reasons,
        "reason_labels": [ORDER_VIABILITY_LABELS.get(reason, reason) for reason in reasons],
        "reason_details": [reason_details[reason] for reason in reasons],
        "summary": "；".join(reason_details[reason] for reason in reasons) if reasons else "订单金额、成本比和预期净利润均达标",
    }


def explain_position_sizing(
    *,
    base_risk_pct: float,
    candidate: dict[str, Any] | None,
    guard: dict[str, Any] | None,
    target: dict[str, Any] | None,
    final_risk_pct: float,
    risk: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate = candidate or {}
    guard = guard or {}
    target = target or {}
    risk_adjustment = candidate.get("risk_adjustment") or {}
    multipliers = {
        "signal": float(risk_adjustment.get("multiplier") or 1.0) if isinstance(risk_adjustment, dict) else 1.0,
        "quality": float(candidate.get("quality_risk_multiplier") or 1.0),
        "live_credit": float(((candidate.get("live_credit_adjustment") or {}).get("risk_multiplier")) or 1.0),
        "equity_guard": float(guard.get("risk_multiplier", 1.0)),
        "target_progress": float(target.get("effective_risk_multiplier", 1.0)),
        "market_state": float(((candidate.get("market_state") or {}).get("risk_multiplier")) or 1.0),
    }
    caps = {
        "max_notional": float((risk or {}).get("max_notional") or 0.0),
        "max_margin": float((risk or {}).get("max_margin") or 0.0),
    }
    reasons = []
    if candidate.get("entry_type_label"):
        reasons.append(f"信号类型：{candidate.get('entry_type_label')}")
    if target.get("reason"):
        reasons.append(f"目标进度：{target.get('reason')}")
    if guard.get("reason"):
        reasons.append(f"权益保护：{guard.get('reason')}")
    reasons.extend([str(item) for item in (((candidate.get("live_credit_adjustment") or {}).get("reasons")) or [])[:3]])
    return {
        "base_risk_pct": round(float(base_risk_pct), 6),
        "final_risk_pct": round(float(final_risk_pct), 6),
        "multipliers": {key: round(value, 6) for key, value in multipliers.items()},
        "caps": caps,
        "reasons": reasons,
    }


def unified_position_sizing(
    *,
    stage_base_risk: float,
    candidate: dict[str, Any] | None,
    guard: dict[str, Any] | None,
    target: dict[str, Any] | None,
    market_state: dict[str, Any] | None = None,
    liquidity: dict[str, Any] | None = None,
    risk_caps: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate = candidate or {}
    guard = guard or {}
    target = target or {}
    market_state = market_state or candidate.get("market_state") or {}
    liquidity = liquidity or candidate.get("depth") or {}
    risk_caps = risk_caps or {}
    risk_adjustment = candidate.get("risk_adjustment") or {}
    live_credit = candidate.get("live_credit_adjustment") or {}
    multipliers = {
        "signal": float(risk_adjustment.get("multiplier") or candidate.get("risk_multiplier") or 1.0),
        "quality": float(candidate.get("quality_risk_multiplier") or 1.0),
        "live_credit": float(live_credit.get("risk_multiplier") or 1.0),
        "target_progress": float(target.get("effective_risk_multiplier") or 1.0),
        "equity_guard": float(guard.get("risk_multiplier") or 1.0),
        "market_state": float(market_state.get("risk_multiplier") or 1.0),
        "liquidity": float(liquidity.get("risk_multiplier") or 1.0),
    }
    raw_risk_pct = float(stage_base_risk)
    for value in multipliers.values():
        raw_risk_pct *= value
    max_risk_pct = float(risk_caps.get("max_risk_pct") or candidate.get("max_risk_pct") or raw_risk_pct)
    min_risk_pct = float(risk_caps.get("min_risk_pct") or 0.0)
    final_risk_pct = max(min_risk_pct, min(raw_risk_pct, max_risk_pct))
    reasons: list[str] = []
    if candidate.get("entry_type_label"):
        reasons.append(f"entry_type={candidate.get('entry_type_label')}")
    if guard.get("reason"):
        reasons.append(f"equity_guard={guard.get('reason')}")
    if target.get("status"):
        reasons.append(f"target={target.get('status')}")
    if market_state.get("label"):
        reasons.append(f"market_state={market_state.get('label')}")
    reasons.extend([str(item) for item in (live_credit.get("reasons") or [])[:4]])
    return {
        "allowed": final_risk_pct > 0 and not guard.get("allowed") is False,
        "stage_base_risk_pct": round(float(stage_base_risk), 6),
        "raw_risk_pct": round(raw_risk_pct, 6),
        "final_risk_pct": round(final_risk_pct, 6),
        "multipliers": {key: round(float(value), 6) for key, value in multipliers.items()},
        "caps": {
            "min_risk_pct": round(min_risk_pct, 6),
            "max_risk_pct": round(max_risk_pct, 6),
            "max_notional": float(risk_caps.get("max_notional") or 0.0),
            "max_margin": float(risk_caps.get("max_margin") or 0.0),
        },
        "reasons": reasons,
    }
