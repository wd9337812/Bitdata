from __future__ import annotations

from typing import Any


PROBE_ENTRY_TYPES = {"extreme_probe", "weak_quality_probe", "preemptive", "momentum", "small_standard", "observe_standard"}


def signal_strength_tier(candidate: dict[str, Any] | None) -> str:
    candidate = candidate or {}
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
    tier = signal_strength_tier(candidate)
    guard_multiplier = max(0.0, float(guard.get("risk_multiplier", 1.0)))
    target_multiplier = max(0.0, float(target.get("effective_risk_multiplier", 1.0)))
    guard_floor = 0.0
    risk_floor = 0.0
    if config.get("effective_position_sizing_enabled", True) and mode == "extreme_sprint":
        guard_floor = float(config.get(f"effective_guard_{tier}_min_multiplier", 0.2))
        guard_multiplier = max(guard_multiplier, guard_floor)
        risk_floor = float(config.get(f"effective_{tier}_min_risk_pct", 0.0))
    calculated = raw_risk * guard_multiplier * target_multiplier
    final_risk = max(calculated, risk_floor) if raw_risk > 0 else 0.0
    max_risk = float(config.get(f"effective_{tier}_max_risk_pct", raw_risk or final_risk))
    if max_risk > 0:
        final_risk = min(final_risk, max_risk)
    return {
        "tier": tier,
        "candidate_risk_pct": round(raw_risk, 8),
        "guard_multiplier": round(guard_multiplier, 6),
        "guard_floor": round(guard_floor, 6),
        "target_multiplier": round(target_multiplier, 6),
        "risk_floor_pct": round(risk_floor, 6),
        "max_risk_pct": round(max_risk, 6),
        "final_risk_pct": round(final_risk, 8),
    }


def effective_order_viability(
    *,
    notional: float,
    candidate: dict[str, Any] | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    candidate = candidate or {}
    expected_profit_pct = max(0.0, float(candidate.get("expected_profit_pct") or 0.0))
    estimated_cost_pct = max(0.0, float(candidate.get("estimated_cost_pct") or 0.0))
    cost_ratio = float(candidate.get("cost_ratio") or (expected_profit_pct / estimated_cost_pct if estimated_cost_pct else 999.0))
    expected_net_profit = float(notional) * max(0.0, expected_profit_pct - estimated_cost_pct) / 100
    min_notional = float(config.get("effective_min_order_notional_usdt", 10.0))
    min_cost_ratio = float(config.get("effective_min_profit_cost_ratio", 3.0))
    min_net_profit = float(config.get("effective_min_net_profit_usdt", 0.15))
    reasons = []
    if float(notional) < min_notional:
        reasons.append("below_effective_min_notional")
    if cost_ratio < min_cost_ratio:
        reasons.append("insufficient_profit_cost_ratio")
    if expected_net_profit < min_net_profit:
        reasons.append("insufficient_expected_net_profit")
    return {
        "allowed": not reasons,
        "notional": round(float(notional), 8),
        "minimum_notional": min_notional,
        "cost_ratio": round(cost_ratio, 6),
        "minimum_cost_ratio": min_cost_ratio,
        "expected_net_profit": round(expected_net_profit, 8),
        "minimum_net_profit": min_net_profit,
        "reasons": reasons,
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
