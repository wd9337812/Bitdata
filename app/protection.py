from __future__ import annotations

from typing import Any


ENTRY_LABELS = {
    "standard": "\u6807\u51c6\u4fdd\u62a4",
    "preemptive": "\u62a2\u8dd1\u4fdd\u62a4",
    "momentum": "\u52a8\u91cf\u4fdd\u62a4",
    "extreme_probe": "\u706b\u836f\u6876\u8bd5\u63a2\u4fdd\u62a4",
    "weak_quality_probe": "\u5f31\u8d28\u91cf\u8bd5\u63a2\u4fdd\u62a4",
    "observe_standard": "\u89c2\u5bdf\u6c60\u8bd5\u5355\u4fdd\u62a4",
    "extreme_scalp": "极限短打保护",
    "orderbook_impact": "盘口冲击保护",
    "volume_scalp": "放量剥头皮保护",
    "imbalance_probe": "失衡试探保护",
}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _profile_from_signal(signal: dict[str, Any]) -> dict[str, Any]:
    profile = signal.get("protection_profile") or {}
    return profile if isinstance(profile, dict) else {}


def _entry_defaults(entry_type: str) -> tuple[float, float, int]:
    if entry_type == "weak_quality_probe":
        return 0.55, 0.75, 3
    if entry_type == "extreme_probe":
        return 0.65, 0.9, 3
    if entry_type == "preemptive":
        return 0.75, 1.0, 4
    if entry_type == "momentum":
        return 0.8, 1.2, 5
    if entry_type == "extreme_scalp":
        return 0.55, 0.75, 2
    if entry_type in {"orderbook_impact", "volume_scalp", "imbalance_probe"}:
        return 0.35, 0.45, 2
    return 1.0, 1.5, 18


def build_protection_plan(
    signal: dict[str, Any],
    config: dict[str, Any],
    *,
    entry_type: str = "standard",
    direction: str | None = None,
) -> dict[str, Any]:
    direction = str(direction or signal.get("signal") or "LONG").upper()
    entry_type = str(entry_type or signal.get("entry_type") or "standard")
    enabled = bool(config.get("dynamic_protection_enabled", True))
    price = _float(signal.get("last_price"))
    atr = _float(signal.get("atr"))
    profile = _profile_from_signal(signal)
    default_stop_atr, default_take_atr, default_hold = _entry_defaults(entry_type)
    stop_atr = _float(profile.get("stop_atr"), default_stop_atr)
    take_profit_atr = _float(profile.get("take_profit_atr"), default_take_atr)
    stop_pct = _float(profile.get("stop_pct"), 0.0)
    take_profit_pct = _float(profile.get("take_profit_pct"), 0.0)
    max_hold_bars = int(profile.get("max_hold_bars") or default_hold)
    if price <= 0 or atr <= 0:
        return {
            "enabled": enabled,
            "entry_type": entry_type,
            "label": ENTRY_LABELS.get(entry_type, ENTRY_LABELS["standard"]),
            "reason": "price_or_atr_missing",
            "initial_stop": signal.get("stop"),
            "initial_take_profit": signal.get("take_profit"),
            "max_hold_bars": max_hold_bars,
        }
    is_short = direction == "SHORT"
    initial_stop = (
        price * (1 + stop_pct / 100)
        if is_short
        else price * (1 - stop_pct / 100)
    ) if stop_pct > 0 else (price + atr * stop_atr if is_short else price - atr * stop_atr)
    initial_take_profit = (
        price * (1 - take_profit_pct / 100)
        if is_short
        else price * (1 + take_profit_pct / 100)
    ) if take_profit_pct > 0 else (price - atr * take_profit_atr if is_short else price + atr * take_profit_atr)
    fast_invalid_atr = _float(
        profile.get("fast_invalid_atr"),
        _float(config.get("protection_fast_invalid_atr", 0.35)),
    )
    fast_invalid_price = price + atr * fast_invalid_atr if is_short else price - atr * fast_invalid_atr
    break_even_trigger_atr = _float(
        profile.get("break_even_trigger_atr"),
        _float(config.get("protection_break_even_trigger_atr", 0.55)),
    )
    trailing_trigger_atr = _float(
        profile.get("trailing_trigger_atr"),
        _float(config.get("protection_trailing_trigger_atr", 0.9)),
    )
    trailing_distance_atr = _float(
        profile.get("trailing_distance_atr"),
        _float(config.get("protection_trailing_distance_atr", 0.55)),
    )
    break_even_buffer_pct = _float(
        profile.get("break_even_buffer_pct"),
        _float(config.get("protection_break_even_buffer_pct", 0.08)),
    )
    break_even_price = (
        price * (1 - break_even_buffer_pct / 100)
        if is_short
        else price * (1 + break_even_buffer_pct / 100)
    )
    return {
        "enabled": enabled,
        "entry_type": entry_type,
        "label": ENTRY_LABELS.get(entry_type, ENTRY_LABELS["standard"]),
        "direction": direction,
        "entry_price": price,
        "atr": atr,
        "stop_atr": stop_atr,
        "take_profit_atr": take_profit_atr,
        "stop_pct": stop_pct,
        "take_profit_pct": take_profit_pct,
        "initial_stop": initial_stop,
        "initial_take_profit": initial_take_profit,
        "fast_invalid": {
            "seconds": int(profile.get("fast_invalid_seconds") or config.get("protection_fast_invalid_seconds", 90)),
            "atr": fast_invalid_atr,
            "price": fast_invalid_price,
        },
        "break_even": {
            "trigger_atr": break_even_trigger_atr,
            "trigger_price": price - atr * break_even_trigger_atr if is_short else price + atr * break_even_trigger_atr,
            "buffer_pct": break_even_buffer_pct,
            "price": break_even_price,
        },
        "trailing": {
            "trigger_atr": trailing_trigger_atr,
            "trigger_price": price - atr * trailing_trigger_atr if is_short else price + atr * trailing_trigger_atr,
            "distance_atr": trailing_distance_atr,
        },
        "max_hold_bars": max_hold_bars,
        "min_profit_after_cost_pct": _float(
            profile.get("min_profit_after_cost_pct"),
            _float(config.get("protection_min_profit_after_cost_pct", 0.08)),
        ),
        "reason": "dynamic_plan_ready",
    }


def apply_initial_protection_to_signal(signal: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    if not plan.get("enabled", True):
        return signal
    updated = dict(signal)
    if plan.get("initial_stop"):
        updated["stop"] = plan["initial_stop"]
    if plan.get("initial_take_profit"):
        updated["take_profit"] = plan["initial_take_profit"]
    updated["protection_plan"] = plan
    updated["protection_profile"] = {
        "stop_atr": plan.get("stop_atr"),
        "take_profit_atr": plan.get("take_profit_atr"),
        "stop_pct": plan.get("stop_pct"),
        "take_profit_pct": plan.get("take_profit_pct"),
        "max_hold_bars": plan.get("max_hold_bars"),
        "break_even_atr": (plan.get("break_even") or {}).get("trigger_atr"),
        "trailing_trigger_atr": (plan.get("trailing") or {}).get("trigger_atr"),
        "trailing_distance_atr": (plan.get("trailing") or {}).get("distance_atr"),
    }
    return updated
