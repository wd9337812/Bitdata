from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


STAGE_PROFILES = [
    {
        "stage": "S0",
        "label": "机会引擎 V3 单仓滚仓",
        "min_equity": 0.0,
        "max_equity": 300.0,
        "recommended_mode": "extreme_sprint",
        "strategy_family": "extreme_v3_roll",
        "risk_posture": "极高风险",
        "max_open_positions": 1,
        "risk_key": "stage_s0_risk_pct",
        "risk_default": 10.0,
        "margin_key": "stage_s0_margin_pct",
        "margin_default": 90.0,
        "leverage_key": "stage_s0_max_leverage",
        "leverage_default": 5.0,
        "daily_loss_key": "stage_s0_daily_loss_limit_pct",
        "daily_loss_default": 30.0,
    },
    {
        "stage": "S1",
        "label": "机会引擎 V3 增强滚仓",
        "min_equity": 300.0,
        "max_equity": 10_000.0,
        "recommended_mode": "extreme_sprint",
        "strategy_family": "extreme_v3_roll",
        "risk_posture": "很高风险",
        "max_open_positions": 1,
        "risk_key": "stage_s1_risk_pct",
        "risk_default": 7.0,
        "margin_key": "stage_s1_margin_pct",
        "margin_default": 85.0,
        "leverage_key": "stage_s1_max_leverage",
        "leverage_default": 5.0,
        "daily_loss_key": "stage_s1_daily_loss_limit_pct",
        "daily_loss_default": 25.0,
    },
    {
        "stage": "S2",
        "label": "机会引擎 V3 组合滚仓",
        "min_equity": 10_000.0,
        "max_equity": 100_000.0,
        "recommended_mode": "extreme_sprint",
        "strategy_family": "extreme_v3_roll",
        "risk_posture": "高风险",
        "max_open_positions": 2,
        "risk_key": "stage_s2_risk_pct",
        "risk_default": 3.0,
        "margin_key": "stage_s2_margin_pct",
        "margin_default": 60.0,
        "leverage_key": "stage_s2_max_leverage",
        "leverage_default": 4.0,
        "daily_loss_key": "stage_s2_daily_loss_limit_pct",
        "daily_loss_default": 12.0,
    },
    {
        "stage": "S3",
        "label": "盘口剥头皮",
        "min_equity": 100_000.0,
        "max_equity": 1_000_000.0,
        "recommended_mode": "yolo_scalp",
        "strategy_family": "orderbook_scalp",
        "risk_posture": "中等风险",
        "max_open_positions": 4,
        "risk_key": "stage_s3_risk_pct",
        "risk_default": 0.35,
        "margin_key": "stage_s3_margin_pct",
        "margin_default": 25.0,
        "leverage_key": "stage_s3_max_leverage",
        "leverage_default": 3.0,
        "daily_loss_key": "stage_s3_daily_loss_limit_pct",
        "daily_loss_default": 3.0,
    },
    {
        "stage": "S4",
        "label": "网格加剥头皮",
        "min_equity": 1_000_000.0,
        "max_equity": float("inf"),
        "recommended_mode": "grid",
        "overlay_mode": "yolo_scalp",
        "strategy_family": "grid_stable",
        "risk_posture": "低到中风险",
        "max_open_positions": 8,
        "risk_key": "stage_s4_risk_pct",
        "risk_default": 0.2,
        "margin_key": "stage_s4_margin_pct",
        "margin_default": 15.0,
        "leverage_key": "stage_s4_max_leverage",
        "leverage_default": 2.0,
        "daily_loss_key": "stage_s4_daily_loss_limit_pct",
        "daily_loss_default": 1.5,
    },
]

_PROFILE_BY_STAGE = {item["stage"]: item for item in STAGE_PROFILES}
_STAGE_INDEX = {item["stage"]: index for index, item in enumerate(STAGE_PROFILES)}
_MANUAL_MODES = {"extreme_sprint", "yolo_scalp", "grid", "attack", "balanced", "conservative"}


def _as_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _profile_values(profile: dict[str, Any], config: dict[str, Any], equity: float) -> dict[str, Any]:
    result = dict(profile)
    if result.get("max_equity") == float("inf"):
        result["max_equity"] = None
    result["equity"] = float(equity)
    result["risk_pct"] = float(config.get(profile["risk_key"], profile["risk_default"]))
    result["base_risk_pct"] = result["risk_pct"]
    result["margin_pct"] = float(config.get(profile["margin_key"], profile["margin_default"]))
    result["leverage"] = float(config.get(profile["leverage_key"], profile["leverage_default"]))
    result["daily_loss_limit_pct"] = float(
        config.get(profile["daily_loss_key"], profile["daily_loss_default"])
    )
    result["max_open_positions"] = int(
        config.get(f"stage_{str(profile['stage']).lower()}_max_open_positions", profile["max_open_positions"])
    )
    result["mode"] = profile["recommended_mode"]
    result["summary"] = (
        f"{result['stage']} {result['label']}：{result['recommended_mode']}，"
        f"单笔风险上限 {result['risk_pct']:.2f}%，最大持仓 {result['max_open_positions']}"
    )
    return result


def stage_profile_for_equity(equity: float | None, config: dict[str, Any] | None = None) -> dict[str, Any]:
    value = float(equity or 0.0)
    config = config or {}
    for profile in STAGE_PROFILES:
        if float(profile["min_equity"]) <= value < float(profile["max_equity"]):
            return _profile_values(profile, config, value)
    return _profile_values(STAGE_PROFILES[-1], config, value)


def stage_profile_by_name(stage: str | None, config: dict[str, Any], equity: float) -> dict[str, Any]:
    profile = _PROFILE_BY_STAGE.get(str(stage or ""), STAGE_PROFILES[0])
    return _profile_values(profile, config, equity)


def all_stage_profiles(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    config = config or {}
    return [_profile_values(item, config, float(item["min_equity"])) for item in STAGE_PROFILES]


def _manual_route(config: dict[str, Any], equity: float, now: datetime) -> dict[str, Any] | None:
    mode = str(config.get("stage_manual_mode") or "auto").lower()
    if mode == "auto" or mode not in _MANUAL_MODES:
        return None
    until = _as_utc(config.get("stage_manual_until"))
    if until is not None and until <= now:
        return None
    profile = stage_profile_for_equity(equity, config)
    family = {
        "extreme_sprint": "extreme_v3_roll",
        "yolo_scalp": "orderbook_scalp",
        "grid": "grid_stable",
    }.get(mode, "legacy_mixed")
    profile.update(
        {
            "mode": mode,
            "recommended_mode": mode,
            "strategy_family": family,
            "source": "manual",
            "manual_until": until.isoformat() if until else None,
            "reason": "manual_override",
        }
    )
    return profile


def resolve_stage_route(
    config: dict[str, Any],
    state: dict[str, Any],
    equity: float | None,
    *,
    has_open_positions: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    value = float(equity or 0.0)
    now = now or datetime.now(timezone.utc)
    if not config.get("stage_routing_enabled", True):
        mode = str(config.get("growth_mode") or "balanced").lower()
        profile = stage_profile_for_equity(value, config)
        return {
            **profile,
            "mode": mode,
            "recommended_mode": mode,
            "source": "disabled",
            "reason": "stage_routing_disabled",
            "pending": False,
            "desired_stage": profile["stage"],
            "confirmation_count": 0,
        }

    manual = _manual_route(config, value, now)
    desired = manual or {**stage_profile_for_equity(value, config), "source": "auto", "reason": "equity_range"}
    current_stage = str(state.get("active_stage") or "")
    current = stage_profile_by_name(current_stage, config, value) if current_stage in _PROFILE_BY_STAGE else None
    if current is not None:
        current_mode = str(
            state.get("active_growth_mode")
            or (state.get("stage_route") or {}).get("mode")
            or current.get("recommended_mode")
        )
        current.update({"mode": current_mode, "recommended_mode": current_mode})

    if manual is None and current is not None and desired["stage"] != current["stage"]:
        current_index = _STAGE_INDEX[current["stage"]]
        desired_index = _STAGE_INDEX[desired["stage"]]
        up_buffer = float(config.get("stage_switch_up_buffer_pct", 5.0)) / 100
        down_buffer = float(config.get("stage_switch_down_buffer_pct", 10.0)) / 100
        qualified = True
        if desired_index > current_index:
            boundary = float(desired["min_equity"])
            qualified = value >= boundary * (1 + up_buffer)
        elif desired_index < current_index:
            boundary = float(current["min_equity"])
            qualified = value <= boundary * (1 - down_buffer)
        if not qualified:
            desired = {**current, "source": "auto", "reason": "hysteresis_hold"}

    target_stage = str(desired["stage"])
    previous_target = str(state.get("stage_route_candidate") or "")
    previous_count = int(state.get("stage_route_confirmation_count") or 0)
    confirmation_count = previous_count + 1 if previous_target == target_stage else 1
    required = max(1, int(config.get("stage_switch_confirmations", 3)))
    if current is None:
        required = 1
    confirmed = confirmation_count >= required or target_stage == current_stage or manual is not None

    active = desired if confirmed or current is None else {**current, "source": "auto", "reason": "awaiting_confirmation"}
    mode_change = bool(current and active.get("recommended_mode") != current.get("recommended_mode"))
    pending = bool(mode_change and has_open_positions)
    if pending:
        active = {**current, "source": desired.get("source", "auto"), "reason": "waiting_for_flat_position"}

    return {
        **active,
        "mode": active.get("recommended_mode"),
        "desired_stage": target_stage,
        "desired_mode": desired.get("recommended_mode"),
        "pending": pending,
        "pending_stage": target_stage if pending else None,
        "pending_mode": desired.get("recommended_mode") if pending else None,
        "confirmation_count": confirmation_count,
        "confirmation_required": required,
        "candidate_stage": target_stage,
        "manual_until": desired.get("manual_until"),
        "updated_at": now.isoformat(),
    }


def stage_route_state_updates(route: dict[str, Any]) -> dict[str, Any]:
    return {
        "active_stage": route.get("stage"),
        "active_growth_mode": route.get("mode"),
        "stage_route": route,
        "stage_route_candidate": route.get("candidate_stage"),
        "stage_route_confirmation_count": route.get("confirmation_count", 0),
        "pending_stage": route.get("pending_stage"),
        "pending_growth_mode": route.get("pending_mode"),
    }


def apply_stage_route(config: dict[str, Any], route: dict[str, Any] | None) -> dict[str, Any]:
    if not route:
        return config
    return {**config, "_stage_route": route, "_active_growth_mode": route.get("mode")}
