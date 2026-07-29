from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.state_store import load_state, save_state
from app.strategy_capabilities import strategy_family_for_version, strategy_supports


STATE_KEY = "s0_continuous_permit"


def s0_continuous_permit_active(config: dict[str, Any]) -> bool:
    route = config.get("_stage_route") or {}
    stage = str(route.get("stage") or "S0").upper()
    version = str(config.get("opportunity_v4_strategy_version") or "").lower()
    return bool(
        config.get("s0_continuous_permit_enabled", True)
        and strategy_supports(version, "continuous_permit")
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


def _level_multiplier(level: int, config: dict[str, Any], *, v50_active: bool = False) -> float:
    version = str(config.get("opportunity_v4_strategy_version") or "").lower()
    if strategy_supports(version, "v511_incident_guard"):
        values = (
            float(config.get("opportunity_v50_loss_3_multiplier", 1.0)),
            float(config.get("opportunity_v511_loss_2_multiplier", 0.25)),
            float(config.get("opportunity_v511_loss_1_multiplier", 0.40)),
            1.0,
        )
        return max(0.0, min(1.0, values[max(0, min(3, int(level)))]))
    prefix = "opportunity_v50_" if v50_active else "s0_continuous_"
    values = (
        float(config.get(f"{prefix}loss_3_multiplier", 1.0 if v50_active else 0.25)),
        float(config.get(f"{prefix}loss_2_multiplier", 0.60 if v50_active else 0.50)),
        float(config.get(f"{prefix}loss_1_multiplier", 0.80 if v50_active else 0.75)),
        1.0,
    )
    return max(0.0, min(1.0, values[max(0, min(3, int(level)))]))


def _base_state(release_id: str, config: dict[str, Any], *, v50_active: bool = False) -> dict[str, Any]:
    version = str(config.get("opportunity_v4_strategy_version") or "").lower()
    initial_multiplier = (
        float(config.get("opportunity_v511_initial_multiplier", 0.50))
        if strategy_supports(version, "v511_incident_guard")
        else float(config.get("opportunity_v50_initial_multiplier", 1.0))
        if v50_active
        else float(config.get("s0_continuous_initial_multiplier", 0.75))
    )
    return {
        "release_id": release_id,
        "status": "initial_exploration",
        "level": 3,
        "risk_multiplier": initial_multiplier,
        "risk_cap_pct": None,
        "cooldown_until": None,
        "post_cooldown_baseline": False,
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


def _trade_close_time(row: dict[str, Any]) -> datetime:
    close_ms = int(row.get("close_time") or 0)
    return (
        datetime.fromtimestamp(close_ms / 1000, timezone.utc)
        if close_ms > 0
        else datetime.now(timezone.utc)
    )


def _parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
    v50_active = strategy_supports(config.get("opportunity_v4_strategy_version"), "v50_s30")
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
            if v50_active and losses >= 3:
                state["cooldown_until"] = (
                    _trade_close_time(row)
                    + timedelta(
                        minutes=float(
                            config.get(
                                "opportunity_v50_loss_3_cooldown_minutes",
                                20.0,
                            )
                        )
                    )
                ).isoformat()
                state["post_cooldown_baseline"] = True
        elif effective_win:
            wins = int(state.get("consecutive_effective_wins") or 0) + 1
            state["consecutive_losses"] = 0
            state["consecutive_effective_wins"] = wins
            state["cooldown_until"] = None
            state["post_cooldown_baseline"] = False
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
    version = str(config.get("opportunity_v4_strategy_version") or "v5.2")
    family = strategy_family_for_version(version)
    release_id = f"{family}@{version}"
    persisted = load_state()
    stored = persisted.get(STATE_KEY)
    state = (
        dict(stored)
        if isinstance(stored, dict) and stored.get("release_id") == release_id
        else _base_state(
            release_id,
            config,
            v50_active=strategy_supports(version, "v50_s30"),
        )
    )
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
    daily_loss_stop_enabled = bool(config.get("stage_s0_daily_loss_stop_enabled", False))

    v50_active = strategy_supports(version, "v50_s30")
    result_multiplier = _level_multiplier(
        int(state.get("level") or 0),
        config,
        v50_active=v50_active,
    )
    if not state.get("processed_trade_ids"):
        result_multiplier = min(
            result_multiplier,
            (
                float(config.get("opportunity_v511_initial_multiplier", 0.50))
                if strategy_supports(version, "v511_incident_guard")
                else float(config.get("opportunity_v50_initial_multiplier", 1.0))
                if v50_active
                else float(config.get("s0_continuous_initial_multiplier", 0.75))
            ),
        )
    daily_cap = 1.0
    if daily_loss_stop_enabled:
        if daily_drawdown_pct >= tier_2:
            daily_cap = float(config.get("s0_continuous_daily_tier_2_multiplier", 0.50))
        elif daily_drawdown_pct >= tier_1:
            daily_cap = float(config.get("s0_continuous_daily_tier_1_multiplier", 0.75))

    cooldown_until = _parse_utc(state.get("cooldown_until"))
    loss_cooldown_active = bool(cooldown_until and now < cooldown_until)
    baseline_risk_cap = (
        float(config.get("opportunity_v50_min_risk_pct", 12.0))
        if v50_active and state.get("post_cooldown_baseline")
        else None
    )

    if hard_stop > 0 and current_equity <= hard_stop:
        allowed = False
        status = "hard_stop"
        reason = f"账户权益 {current_equity:.4f}U 已触发 {hard_stop:.2f}U 硬停止线"
    elif loss_cooldown_active:
        allowed = False
        status = "loss_cooldown"
        reason = (
            f"连续 3 次净亏损后短冷却至 {cooldown_until.isoformat()}；"
            "冷却结束会自动恢复，不需要影子 PF 或人工签发许可证"
        )
    elif daily_loss_stop_enabled and daily_start > 0 and daily_drawdown_pct >= pause_at:
        allowed = False
        status = "daily_paused"
        reason = f"当日从 {daily_start:.4f}U 回撤 {daily_drawdown_pct:.2f}%，达到 {pause_at:.2f}% 暂停线"
    else:
        allowed = True
        multiplier = min(result_multiplier, daily_cap)
        status = (
            "baseline_recovery"
            if baseline_risk_cap is not None
            else "initial_exploration"
            if (
                not state.get("processed_trade_ids")
                and strategy_supports(version, "v511_incident_guard")
            )
            else "normal"
            if multiplier >= 0.999
            else "position_penalty"
        )
        if status == "initial_exploration":
            reason = (
                f"{version.upper()} 新版本首轮受限试探，风险倍率 {multiplier:.2f}x；"
                "建立扣费后盈利证据后自动恢复进攻仓位"
            )
        elif status == "normal":
            reason = "连续准入正常；候选仍需通过触发、扣费后期望和流动性硬门"
        elif status == "baseline_recovery":
            reason = f"短冷却已结束，自动恢复开仓；下一笔压力风险暂时上限 {baseline_risk_cap:.2f}%"
        else:
            reason = f"普通亏损仅把下一笔仓位限制为 {multiplier:.2f}x，不撤销当前版本开仓权"

    multiplier = min(result_multiplier, daily_cap) if allowed else 0.0
    state.update(
        {
            "status": status,
            "risk_multiplier": round(multiplier, 6),
            "risk_cap_pct": round(baseline_risk_cap, 6) if baseline_risk_cap is not None else None,
        }
    )
    status_changed = (
        not isinstance(stored, dict)
        or stored.get("release_id") != release_id
        or str((stored or {}).get("status") or "") != status
        or float((stored or {}).get("risk_multiplier") or 0.0) != round(multiplier, 6)
        or (stored or {}).get("risk_cap_pct") != state.get("risk_cap_pct")
    )
    if changed or status_changed:
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
        "daily_loss_stop_enabled": daily_loss_stop_enabled,
        "cooldown_until": cooldown_until.isoformat() if cooldown_until else None,
        "loss_cooldown_active": loss_cooldown_active,
        "risk_cap_pct": round(baseline_risk_cap, 6) if baseline_risk_cap is not None else None,
        "thresholds": {
            "daily_tier_1_pct": tier_1,
            "daily_tier_2_pct": tier_2,
            "daily_pause_pct": pause_at,
            "hard_stop_equity": hard_stop,
        },
        "recovery_rule": (
            "一次亏损降至 0.80x，两次降至 0.60x；三次后冷却 20 分钟并以 "
            "12% 风险上限自动恢复，有效盈利后解除"
        ),
    }
