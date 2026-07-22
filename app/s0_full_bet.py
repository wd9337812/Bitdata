from __future__ import annotations

from typing import Any


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def s0_full_bet_profile_active(config: dict[str, Any]) -> bool:
    route = config.get("_stage_route") or {}
    stage = str(route.get("stage") or "S0").upper()
    version = str(config.get("opportunity_v4_strategy_version") or "").lower()
    return bool(
        config.get("opportunity_v44_full_bet_enabled", True)
        and version.startswith(("v4.4", "v4.5", "v4.6", "v4.7", "v4.8", "v4.9"))
        and stage == "S0"
    )


def is_s0_full_bet(candidate: dict[str, Any] | None, config: dict[str, Any]) -> bool:
    if not s0_full_bet_profile_active(config):
        return False
    candidate = candidate or {}
    opportunity = candidate.get("opportunity_v4") or {}
    version = str(
        opportunity.get("strategy_version")
        or candidate.get("strategy_version")
        or config.get("opportunity_v4_strategy_version")
        or ""
    ).lower()
    return bool(version.startswith(("v4.4", "v4.5", "v4.6", "v4.7", "v4.8", "v4.9")) and opportunity.get("full_bet_admitted"))


def build_s0_full_bet_sizing(
    *,
    equity: float,
    available_balance: float | None,
    entry: float,
    stop: float,
    requested_risk_pct: float,
    candidate: dict[str, Any] | None,
    config: dict[str, Any],
    consecutive_losses: int = 0,
    account_projection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Use almost all available margin while bounding loss at the protected stop.

    "Full bet" describes capital allocation, not unlimited exchange leverage. The
    leverage is selected from the stop distance and stressed round-trip costs.
    """
    if not is_s0_full_bet(candidate, config):
        return {"enabled": False, "applied": False, "reason": "profile_not_active"}
    equity = max(0.0, float(equity))
    entry = max(0.0, float(entry))
    stop = max(0.0, float(stop))
    if equity <= 0 or entry <= 0 or stop <= 0 or entry == stop:
        return {"enabled": True, "applied": False, "reason": "invalid_sizing_input"}

    available = float(available_balance) if available_balance is not None else equity
    capital = min(equity, max(0.0, available))
    margin_pct = _clamp(float(config.get("opportunity_v44_margin_pct", 90.0)), 1.0, 99.0)
    margin_budget = capital * margin_pct / 100

    opportunity = (candidate or {}).get("opportunity_v4") or {}
    observed_cost_pct = max(
        float(opportunity.get("estimated_cost_pct") or 0.0),
        float((candidate or {}).get("estimated_cost_pct") or 0.0),
        float(config.get("taker_fee_pct_round_trip", 0.08))
        + 2 * float(config.get("estimated_slippage_pct", 0.03)),
    )
    cost_stress = max(1.0, float(config.get("opportunity_v44_cost_stress_multiplier", 1.5)))
    stressed_cost_pct = observed_cost_pct * cost_stress
    stop_distance_pct = abs(entry - stop) / entry * 100
    stressed_loss_per_notional_pct = stop_distance_pct + stressed_cost_pct

    hard_risk_cap = max(0.01, float(config.get("opportunity_v44_stressed_risk_cap_pct", 15.0)))
    maximum_risk = min(
        hard_risk_cap,
        max(0.01, float(config.get("opportunity_v44_max_risk_pct", 15.0))),
    )
    minimum_risk = min(
        maximum_risk,
        max(0.01, float(config.get("opportunity_v44_min_risk_pct", 8.0))),
    )
    target_risk = _clamp(float(requested_risk_pct), 0.0, maximum_risk)
    version = str(config.get("opportunity_v4_strategy_version") or "").lower()
    if not version.startswith(("v4.5", "v4.6", "v4.7", "v4.8", "v4.9")) and int(consecutive_losses) >= int(config.get("opportunity_v44_loss_reduced_after", 2)):
        target_risk = min(target_risk, float(config.get("opportunity_v44_loss_reduced_risk_pct", 8.0)))
    safety_cap_active = target_risk + 1e-9 < minimum_risk

    min_leverage = max(1, int(config.get("opportunity_v44_min_leverage", 3)))
    max_leverage = max(min_leverage, int(config.get("opportunity_v44_max_leverage", 10)))
    selected_leverage = min_leverage
    for leverage in range(min_leverage, max_leverage + 1):
        projected = (
            margin_budget * leverage * stressed_loss_per_notional_pct / equity
            if equity > 0
            else hard_risk_cap
        )
        if projected <= target_risk + 1e-9:
            selected_leverage = leverage
        else:
            break

    notional = margin_budget * selected_leverage
    stressed_risk_pct = notional * stressed_loss_per_notional_pct / equity if equity > 0 else 0.0
    effective_cap = min(hard_risk_cap, target_risk)
    margin_reduced_for_risk = stressed_risk_pct > effective_cap + 1e-9
    if stressed_risk_pct > effective_cap and stressed_loss_per_notional_pct > 0:
        notional = equity * effective_cap / stressed_loss_per_notional_pct
        stressed_risk_pct = effective_cap

    margin_used = notional / selected_leverage if selected_leverage > 0 else 0.0
    reserve = max(0.0, capital - margin_used)
    return {
        "enabled": True,
        "applied": True,
        "profile": "s0_full_bet_v48" if version.startswith("v4.8") else ("s0_full_bet_v47" if version.startswith("v4.7") else ("s0_full_bet_v46" if version.startswith("v4.6") else ("s0_full_bet_v45" if version.startswith("v4.5") else "s0_full_bet_v44"))),
        "margin_budget_pct": round(margin_pct, 6),
        "sizing_available_balance": round(available, 8),
        "sizing_available_balance_source": (account_projection or {}).get("available_balance_source"),
        "sizing_available_balance_age_seconds": (account_projection or {}).get("available_balance_age_seconds"),
        "margin_budget": round(margin_budget, 8),
        "margin_used": round(margin_used, 8),
        "margin_utilization_pct": round(margin_used / capital * 100, 6) if capital > 0 else 0.0,
        "reserve": round(reserve, 8),
        "leverage": selected_leverage,
        "notional": round(max(0.0, notional), 8),
        "quantity": round(max(0.0, notional / entry), 12),
        "target_risk_pct": round(target_risk, 6),
        "stressed_risk_pct": round(stressed_risk_pct, 6),
        "hard_risk_cap_pct": round(hard_risk_cap, 6),
        "stop_distance_pct": round(stop_distance_pct, 6),
        "observed_cost_pct": round(observed_cost_pct, 6),
        "stressed_cost_pct": round(stressed_cost_pct, 6),
        "safety_cap_active": safety_cap_active,
        "margin_reduced_for_risk": margin_reduced_for_risk,
        "consecutive_losses": int(consecutive_losses),
        "reason": "risk_capped_reduced_margin" if margin_reduced_for_risk else "full_margin_dynamic_leverage",
    }


def full_bet_rotation_required_edge_r(config: dict[str, Any]) -> float:
    return max(0.0, float(config.get("opportunity_v44_rotation_min_edge_r", 0.35)))
