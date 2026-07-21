from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.state_store import load_state, save_state


STATE_KEY = "s0_continuous_permit"


def s0_continuous_permit_active(config: dict[str, Any]) -> bool:
    route = config.get("_stage_route") or {}
    stage = str(route.get("stage") or "S0").upper()
    version = str(config.get("opportunity_v4_strategy_version") or "").lower()
    return bool(
        config.get("s0_continuous_permit_enabled", True)
        and version.startswith(("v4.5", "v4.6", "v4.7"))
        and stage == "S0"
    )


def _loss_level(consecutive_losses: int) -> int:
    if consecutive_losses >= 3:
        return 0
    if consecutive_losses == 2:
        return 1
    if consecutive_losses == 1:
        return 2
    return 3


def _level_multiplier(level: int, config: dict[str, Any]) -> float:
    values = (
        float(config.get("s0_continuous_loss_3_multiplier", 0.25)),
        float(config.get("s0_continuous_loss_2_multiplier", 0.50)),
        float(config.get("s0_continuous_loss_1_multiplier", 0.75)),
        1.0,
    )
    return max(0.0, min(1.0, values[max(0, min(3, int(level)))]))


def _base_state(release_id: str, config: dict[str, Any]) -> dict[str, Any]:
    return {
        "release_id": release_id,
        "status": "initial_exploration",
        "level": 2,
        "risk_multiplier": float(config.get("s0_continuous_initial_multiplier", 0.75)),
        "consecutive_losses": 0,
        "consecutive_effective_wins": 0,
        "processed_trade_ids": [],
        "last_processed_close_time": 0,
        "last_result": None,
        "updated_at": None,
    }


def _trade_is_effective_win(row: dict[str, Any], config: dict[str, Any]) -> tuple[bool, bool]:
    net_pnl = float(row.get("net_pnl") or 0.0)
    cost = abs(float(row.get("commission") or 0.0)) + abs(float(row.get("funding_fee") or 0.0))
    minimum_multiple = float(config.get("s0_continuous_profit_recovery_cost_multiple", 2.0))
    strong_multiple = float(config.get("s0_continuous_strong_profit_cost_multiple", 6.0))
    effective = net_pnl > 0 and (cost <= 0 or net_pnl >= cost * minimum_multiple)
    strong = effective and (cost <= 0 or net_pnl >= cost * strong_multiple)
    return effective, strong


def _reconcile_results(
    state: dict[str, Any],
    live_rows: list[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    processed = {int(value) for value in (state.get("processed_trade_ids") or []) if value is not None}
    rows = sorted(
        (dict(row) for row in live_rows if int(row.get("id") or 0) not in processed),
        key=lambda row: (int(row.get("close_time") or 0), int(row.get("id") or 0)),
    )
    changed = False
    for row in rows:
        trade_id = int(row.get("id") or 0)
        net_pnl = float(row.get("net_pnl") or 0.0)
        effective_win, strong_win = _trade_is_effective_win(row, config)
        if net_pnl <= 0:
            losses = int(state.get("consecutive_losses") or 0) + 1
            state["consecutive_losses"] = losses
            state["consecutive_effective_wins"] = 0
            state["level"] = _loss_level(losses)
            state["last_result"] = "loss"
        elif effective_win:
            wins = int(state.get("consecutive_effective_wins") or 0) + 1
            state["consecutive_losses"] = 0
            state["consecutive_effective_wins"] = wins
            if strong_win or wins >= int(config.get("s0_continuous_full_recovery_wins", 2)):
                state["level"] = 3
                state["last_result"] = "strong_win"
            else:
                state["level"] = min(3, int(state.get("level") or 0) + 1)
                state["last_result"] = "effective_win"
        else:
            state["consecutive_effective_wins"] = 0
            state["last_result"] = "cost_drag_win"
        if trade_id:
            processed.add(trade_id)
        state["last_processed_close_time"] = max(
            int(state.get("last_processed_close_time") or 0),
            int(row.get("close_time") or 0),
        )
        changed = True
    state["processed_trade_ids"] = sorted(processed)[-200:]
    return state, changed


def s0_continuous_permit_status(
    config: dict[str, Any],
    *,
    equity: float | None,
    live_rows: list[dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    version = str(config.get("opportunity_v4_strategy_version") or "v4.7")
    release_id = f"extreme_v4_roll@{version}"
    persisted = load_state()
    stored = persisted.get(STATE_KEY)
    state = dict(stored) if isinstance(stored, dict) and stored.get("release_id") == release_id else _base_state(release_id, config)
    state, changed = _reconcile_results(state, live_rows, config)

    current_equity = float(equity or 0.0)
    daily_start = float(persisted.get("daily_start_equity") or current_equity)
    daily_drawdown_pct = (
        max(0.0, (daily_start - current_equity) / daily_start * 100.0)
        if daily_start > 0 and current_equity > 0
        else 0.0
    )
    hard_stop = float(config.get("hard_stop_equity", config.get("tournament_stop_equity", 5.0)))
    tier_1 = float(config.get("s0_continuous_daily_tier_1_pct", 10.0))
    tier_2 = float(config.get("s0_continuous_daily_tier_2_pct", 20.0))
    pause_at = float(config.get("s0_continuous_daily_pause_pct", 30.0))

    result_multiplier = _level_multiplier(int(state.get("level") or 0), config)
    if not state.get("processed_trade_ids"):
        result_multiplier = min(result_multiplier, float(config.get("s0_continuous_initial_multiplier", 0.75)))
    daily_cap = 1.0
    if daily_drawdown_pct >= tier_2:
        daily_cap = float(config.get("s0_continuous_daily_tier_2_multiplier", 0.50))
    elif daily_drawdown_pct >= tier_1:
        daily_cap = float(config.get("s0_continuous_daily_tier_1_multiplier", 0.75))

    if hard_stop > 0 and current_equity <= hard_stop:
        allowed = False
        status = "hard_stop"
        reason = f"账户权益 {current_equity:.4f}U 已触发 {hard_stop:.2f}U 硬停止线"
    elif daily_start > 0 and daily_drawdown_pct >= pause_at:
        allowed = False
        status = "daily_paused"
        reason = f"当日从 {daily_start:.4f}U 回撤 {daily_drawdown_pct:.2f}%，达到 {pause_at:.2f}% 暂停线"
    else:
        allowed = True
        multiplier = min(result_multiplier, daily_cap)
        status = "normal" if multiplier >= 0.999 else "position_penalty"
        reason = (
            "连续准入正常，候选仍须通过触发、扣费后期望和流动性硬门"
            if status == "normal"
            else f"普通亏损或当日回撤仅把仓位限制为 {multiplier:.2f}x，不暂停其他合格机会"
        )

    multiplier = min(result_multiplier, daily_cap) if allowed else 0.0
    status_changed = (
        str(state.get("status") or "") != status
        or float(state.get("risk_multiplier") or 0.0) != round(multiplier, 6)
    )
    state.update({"status": status, "risk_multiplier": round(multiplier, 6)})
    if changed or status_changed or not isinstance(stored, dict) or stored.get("release_id") != release_id:
        state["updated_at"] = now.isoformat()
        save_state({STATE_KEY: state})
    return {
        **state,
        "enabled": True,
        "allowed": allowed,
        "blocks_new_entries": not allowed,
        "reason": reason,
        "daily_start_equity": round(daily_start, 8),
        "current_equity": round(current_equity, 8),
        "daily_drawdown_pct": round(daily_drawdown_pct, 6),
        "daily_cap_multiplier": round(daily_cap, 6),
        "thresholds": {
            "daily_tier_1_pct": tier_1,
            "daily_tier_2_pct": tier_2,
            "daily_pause_pct": pause_at,
            "hard_stop_equity": hard_stop,
        },
        "recovery_rule": "扣费后有效盈利提升一级；强盈利或连续两次有效盈利恢复 1.00x",
    }
