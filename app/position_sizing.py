from __future__ import annotations

from typing import Any


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
