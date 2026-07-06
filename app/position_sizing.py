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
