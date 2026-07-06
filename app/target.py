from __future__ import annotations

from datetime import datetime, timezone
from math import pow
from typing import Any


PHASES = [
    ("A", "target_phase_a_equity", "30天到10000U"),
    ("B", "target_phase_b_equity", "再30天到100000U"),
    ("C", "target_phase_c_equity", "再30天到1000000U"),
]


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def target_phases(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"phase": code, "target_equity": float(config.get(key, 0) or 0), "label": label}
        for code, key, label in PHASES
    ]


def active_target_phase(config: dict[str, Any], equity: float | None) -> dict[str, Any]:
    current = float(equity or 0)
    for phase in target_phases(config):
        if current < float(phase["target_equity"]):
            return phase
    return {"phase": "GRID", "target_equity": current, "label": "100万以后进入稳定网格/组合阶段"}


def stage_name_for_equity(equity: float | None) -> str:
    value = float(equity or 0)
    if value < 100:
        return "S0 极限滚仓"
    if value < 500:
        return "S1 极限冲刺增强"
    if value < 2_000:
        return "S2 进攻轮动"
    if value < 10_000:
        return "S3 趋势/事件混合"
    if value < 100_000:
        return "S4 受控动量"
    if value < 1_000_000:
        return "S5 多策略组合"
    return "S6 网格稳定"


def target_progress(
    config: dict[str, Any],
    state: dict[str, Any],
    account_summary: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    equity = account_summary.get("equity")
    if equity is None:
        return {
            "enabled": bool(config.get("target_controller_enabled", True)),
            "status": "account_unavailable",
            "risk_multiplier": 1.0,
            "effective_risk_multiplier": 1.0,
            "reason": "账户权益不可用",
        }

    equity = float(equity)
    phase = active_target_phase(config, equity)
    phase_code = str(phase["phase"])
    if phase_code == "GRID":
        return {
            "enabled": bool(config.get("target_controller_enabled", True)),
            "phase": phase_code,
            "phase_label": phase["label"],
            "stage": stage_name_for_equity(equity),
            "status": "completed",
            "equity": round(equity, 8),
            "target_equity": round(float(phase["target_equity"]), 8),
            "risk_multiplier": 1.0,
            "effective_risk_multiplier": 1.0,
            "reason": "已达到100万后稳定阶段",
        }

    phase_days = int(config.get("target_phase_days", 30))
    start_equity_key = f"target_phase_{phase_code.lower()}_start_equity"
    start_time_key = f"target_phase_{phase_code.lower()}_start_time"
    start_equity = float(state.get(start_equity_key) or state.get("target_start_equity") or equity)
    start_time = _parse_time(state.get(start_time_key) or state.get("target_start_time")) or now
    elapsed_days = max(0.0, (now - start_time).total_seconds() / 86400)
    remaining_days = max(0.0, phase_days - elapsed_days)
    target_equity = float(phase["target_equity"])
    total_gain_needed = max(target_equity / max(start_equity, 1e-9), 1.0)
    expected_equity = start_equity * pow(total_gain_needed, min(elapsed_days, phase_days) / phase_days)
    progress_ratio = equity / expected_equity if expected_equity > 0 else 0.0
    target_completion_ratio = equity / target_equity if target_equity > 0 else 0.0
    required_daily_return_pct = (
        (pow(target_equity / max(equity, 1e-9), 1 / max(remaining_days, 1e-9)) - 1) * 100
        if equity < target_equity and remaining_days > 0
        else 0.0
    )
    gap_pct = (1 - progress_ratio) * 100
    ahead_gap_pct = float(config.get("target_ahead_gap_pct", 15.0))
    critical_gap_pct = float(config.get("target_critical_gap_pct", 35.0))

    if progress_ratio >= 1 + ahead_gap_pct / 100:
        status = "ahead"
        multiplier = float(config.get("target_progress_ahead_multiplier", 0.75))
        reason = "进度领先，降低风险保护成果"
    elif progress_ratio >= 0.95:
        status = "on_track"
        multiplier = float(config.get("target_progress_on_track_multiplier", 1.0))
        reason = "进度接近目标曲线，使用标准风险"
    elif gap_pct >= critical_gap_pct:
        status = "critical"
        multiplier = float(config.get("target_progress_critical_multiplier", 1.5))
        reason = "进度严重落后，进入更激进机会档"
    else:
        status = "behind"
        multiplier = float(config.get("target_progress_behind_multiplier", 1.2))
        reason = "进度落后，适度提高机会优先级"

    multiplier = max(
        float(config.get("target_progress_min_multiplier", 0.5)),
        min(multiplier, float(config.get("target_progress_max_multiplier", 1.8))),
    )
    hard_floor = start_equity * float(config.get("target_hard_floor_pct", 35.0)) / 100
    hard_floor_hit = hard_floor > 0 and equity <= hard_floor

    return {
        "enabled": bool(config.get("target_controller_enabled", True)),
        "risk_adjustment_enabled": bool(config.get("target_risk_adjustment_enabled", False)),
        "phase": phase_code,
        "phase_label": phase["label"],
        "stage": stage_name_for_equity(equity),
        "status": status,
        "equity": round(equity, 8),
        "start_equity": round(start_equity, 8),
        "target_equity": round(target_equity, 8),
        "expected_equity": round(expected_equity, 8),
        "progress_ratio": round(progress_ratio, 6),
        "target_completion_ratio": round(target_completion_ratio, 6),
        "elapsed_days": round(elapsed_days, 4),
        "remaining_days": round(remaining_days, 4),
        "required_daily_return_pct": round(required_daily_return_pct, 4),
        "risk_multiplier": round(multiplier, 6),
        "effective_risk_multiplier": round(multiplier if config.get("target_risk_adjustment_enabled", False) else 1.0, 6),
        "hard_floor": round(hard_floor, 8),
        "hard_floor_hit": hard_floor_hit,
        "reason": "触及目标硬底线，禁止继续放大风险" if hard_floor_hit else reason,
    }


def target_state_updates(config: dict[str, Any], state: dict[str, Any], account_summary: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    equity = account_summary.get("equity")
    if equity is None or not config.get("target_controller_enabled", True):
        return {}
    phase = active_target_phase(config, float(equity))
    phase_code = str(phase["phase"]).lower()
    if phase_code == "grid":
        return {}
    updates: dict[str, Any] = {"target_active_phase": phase["phase"]}
    start_equity_key = f"target_phase_{phase_code}_start_equity"
    start_time_key = f"target_phase_{phase_code}_start_time"
    if not state.get(start_equity_key):
        updates[start_equity_key] = float(equity)
    if not state.get(start_time_key):
        updates[start_time_key] = now.isoformat()
    return updates
