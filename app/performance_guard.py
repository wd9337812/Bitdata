from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.local_circuit import reconcile_v4_local_circuit
from app.recovery_controller import recovery_permit_status
from app.s0_continuous_permit import s0_continuous_permit_active, s0_continuous_permit_status
from app.strategy_canary import strategy_canary_status
from app.strategy_releases import (
    ACTIVE_ROLE,
    V4_FAMILY,
    active_family,
    active_release_version,
    ensure_live_release_columns,
    ensure_shadow_release_columns,
    migrate_shadow_release_metadata,
)
from app.telemetry import connect, db_path
from app.v4_evidence import (
    executable_single_position_shadows,
    filter_live_eligible_v4_shadows,
    live_evidence_lanes,
)


_CACHE: dict[str, tuple[float, Any]] = {}


def _profit_factor(positive: float, negative: float) -> float:
    if negative < 0:
        return positive / abs(negative)
    return 999.0 if positive > 0 else 0.0


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    positive = sum(float(row.get("net_pnl") or 0) for row in rows if float(row.get("net_pnl") or 0) > 0)
    negative = sum(float(row.get("net_pnl") or 0) for row in rows if float(row.get("net_pnl") or 0) < 0)
    wins = sum(1 for row in rows if float(row.get("net_pnl") or 0) > 0)
    costs = sum(
        abs(float(row.get("commission") or row.get("estimated_cost") or 0))
        + abs(float(row.get("funding_fee") or 0))
        for row in rows
    )
    return {
        "trades": len(rows),
        "wins": wins,
        "win_rate": round(wins / len(rows) * 100, 2) if rows else 0.0,
        "net_pnl": round(positive + negative, 8),
        "cost": round(costs, 8),
        "profit_factor": round(_profit_factor(positive, negative), 4),
    }


def _cached(key: str, ttl_seconds: float, loader: Any) -> Any:
    cache_key = f"{db_path()}:{key}"
    now = time.monotonic()
    cached = _CACHE.get(cache_key)
    if cached and now - cached[0] <= ttl_seconds:
        return cached[1]
    value = loader()
    _CACHE[cache_key] = (now, value)
    return value


def clear_performance_cache() -> None:
    _CACHE.clear()


def update_release_equity_guard(
    config: dict[str, Any],
    equity: float | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Track one release peak and latch its configured fallback threshold."""
    from app.state_store import load_state, save_state

    now = now or datetime.now(timezone.utc)
    release_id = f"{active_family(config)}@{active_release_version(config)}"
    state = load_state()
    previous = dict(state.get("strategy_release_equity_guard") or {})
    current_equity = max(0.0, float(equity or 0.0))
    reset = str(previous.get("release_id") or "") != release_id
    baseline = current_equity if reset else max(0.0, float(previous.get("baseline_equity") or current_equity))
    peak = current_equity if reset else max(current_equity, float(previous.get("peak_equity") or current_equity))
    drawdown = max(0.0, (peak - current_equity) / peak * 100) if peak > 0 else 0.0
    version = active_release_version(config).lower()
    threshold = float(
        config.get("opportunity_v44_release_pause_drawdown_pct", 35.0)
        if version.startswith(("v4.4", "v4.5", "v4.6", "v4.7", "v4.8", "v4.9", "v4.10", "v4.11"))
        else config.get("opportunity_v432_release_fallback_drawdown_pct", 8.0)
    )
    fallback_active = bool(False if reset else previous.get("fallback_active")) or drawdown >= threshold
    result = {
        "release_id": release_id,
        "baseline_equity": round(baseline, 8),
        "peak_equity": round(peak, 8),
        "current_equity": round(current_equity, 8),
        "drawdown_pct": round(drawdown, 6),
        "fallback_threshold_pct": round(threshold, 6),
        "fallback_active": fallback_active,
        "updated_at": now.isoformat(),
    }
    meaningful_change = bool(
        reset
        or peak > float(previous.get("peak_equity") or 0.0)
        or fallback_active != bool(previous.get("fallback_active"))
    )
    if meaningful_change:
        save_state({"strategy_release_equity_guard": result})
    return result


def global_performance_guard(
    config: dict[str, Any],
    equity: float | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not config.get("performance_guard_enabled", True):
        return {"enabled": False, "allowed": True, "status": "disabled", "risk_multiplier": 1.0}
    now = now or datetime.now(timezone.utc)
    live_limit = int(config.get("performance_guard_live_window_trades", 10))
    shadow_limit = int(config.get("performance_guard_shadow_window_trades", 100))
    recovery_shadow_limit = int(config.get("performance_guard_recovery_shadow_trades", 20))
    current_family = active_family(config)
    current_version = active_release_version(config)
    release_only = bool(config.get("performance_guard_current_release_only", True))
    release_equity_guard = dict(config.get("_strategy_release_equity_guard") or {})

    def load() -> dict[str, Any]:
        with connect() as conn:
            live_scope = "all_strategies"
            try:
                ensure_live_release_columns(conn)
                scoped_live = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT id, symbol, direction, open_time, close_time, net_pnl, commission, funding_fee, payload "
                        "FROM live_trade_records "
                        "WHERE strategy_family = ? AND strategy_version = ? AND strategy_role = ? "
                        "ORDER BY close_time DESC LIMIT ?",
                        (current_family, current_version, ACTIVE_ROLE, live_limit),
                    ).fetchall()
                ]
                scoped_live_total = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM live_trade_records WHERE strategy_family = ? "
                        "AND strategy_version = ? AND strategy_role = ?",
                        (current_family, current_version, ACTIVE_ROLE),
                    ).fetchone()[0]
                )
                fallback_live = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT id, symbol, direction, open_time, close_time, net_pnl, commission, funding_fee, payload "
                        "FROM live_trade_records "
                        "ORDER BY close_time DESC LIMIT ?",
                        (live_limit,),
                    ).fetchall()
                ]
                if release_only and (current_family == V4_FAMILY or scoped_live):
                    live = scoped_live
                    live_scope = f"{current_family}@{current_version}"
                else:
                    live = fallback_live
                    live_scope = "legacy_safety_fallback" if release_only else "all_strategies"
            except sqlite3.OperationalError:
                live = []
                scoped_live = []
                scoped_live_total = 0
                fallback_live = []
                live_scope = "unavailable"
            shadow_scope = "all_strategies"
            try:
                ensure_shadow_release_columns(conn)
                migrate_shadow_release_metadata(conn, config)
                # Diagnostic-only V4 shadows must never grant or veto a live permit.
                # Fetch a bounded release slice, then keep one independent opportunity
                # from a lane that could actually have reached live execution.
                decision_only = current_family == V4_FAMILY
                shadow_evidence_clause = (
                    " AND COALESCE(evidence_type, 'decision') = 'decision'"
                    if decision_only
                    else ""
                )
                shadow_fetch_limit = max(2_000, max(shadow_limit, recovery_shadow_limit) * 20)
                scoped_shadow_raw = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT id, opened_at, closed_at, symbol, direction, net_pnl, estimated_cost, "
                        "opportunity_id, payload FROM shadow_trades "
                        "WHERE status = 'CLOSED' AND strategy_family = ? AND strategy_version = ? "
                        f"AND strategy_role = ?{shadow_evidence_clause} ORDER BY id DESC LIMIT ?",
                        (current_family, current_version, ACTIVE_ROLE, shadow_fetch_limit),
                    ).fetchall()
                ]
                scoped_shadow = filter_live_eligible_v4_shadows(
                    scoped_shadow_raw,
                    strategy_version=current_version,
                    allow_unclassified_legacy=not current_version.startswith(("v4.3", "v4.4", "v4.5", "v4.6", "v4.7", "v4.8", "v4.9", "v4.10", "v4.11")),
                )
                if current_version.startswith(("v4.5", "v4.6", "v4.7", "v4.8", "v4.9", "v4.10", "v4.11")):
                    scoped_shadow = executable_single_position_shadows(scoped_shadow)
                scoped_shadow_total = len(scoped_shadow)
                if release_only and (current_family == V4_FAMILY or scoped_shadow):
                    shadow = scoped_shadow
                    shadow_scope = (
                        f"{current_family}@{current_version}:live_lanes"
                        if decision_only
                        else f"{current_family}@{current_version}"
                    )
                else:
                    shadow = [
                        dict(row)
                        for row in conn.execute(
                            "SELECT id, opened_at, closed_at, symbol, direction, net_pnl, estimated_cost, "
                            "opportunity_id, payload FROM shadow_trades "
                            "WHERE status = 'CLOSED' ORDER BY id DESC LIMIT ?",
                            (max(shadow_limit, recovery_shadow_limit),),
                        ).fetchall()
                    ]
                    shadow_scope = "legacy_safety_fallback" if release_only else "all_strategies"
            except sqlite3.OperationalError:
                shadow = []
                scoped_shadow = []
                scoped_shadow_total = 0
                shadow_scope = "unavailable"
            peak_equity = None
            try:
                peak_cutoff = (
                    now - timedelta(hours=float(config.get("performance_guard_peak_lookback_hours", 24)))
                ).isoformat()
                row = conn.execute(
                    "SELECT MAX(equity) AS peak_equity FROM equity_snapshots WHERE ts >= ? AND equity > 0",
                    (peak_cutoff,),
                ).fetchone()
                peak_equity = float(row["peak_equity"] or 0) if row else None
            except sqlite3.OperationalError:
                peak_equity = None
        return {
            "live_rows": live[:live_limit],
            "current_live_rows": scoped_live[:live_limit],
            "current_live_total": scoped_live_total,
            "fallback_live_rows": fallback_live[:live_limit],
            "shadow_rows": shadow[:shadow_limit],
            "shadow_recovery_rows": shadow[:recovery_shadow_limit],
            "shadow_closed_total": scoped_shadow_total if release_only else len(shadow),
            "peak_equity": peak_equity,
            "live_scope": live_scope,
            "shadow_scope": shadow_scope,
        }

    cache_key = f"global:{current_family}:{current_version}:{int(release_only)}:{live_limit}:{shadow_limit}:{recovery_shadow_limit}"
    raw = _cached(cache_key, float(config.get("performance_guard_cache_seconds", 15)), load)
    live_rows = raw["live_rows"]
    shadow_rows = raw["shadow_rows"]
    live = _stats(live_rows)
    current_live_rows = raw.get("current_live_rows") or []
    current_live = _stats(current_live_rows)
    current_live["latest_net_pnl"] = float(current_live_rows[0].get("net_pnl") or 0) if current_live_rows else 0.0
    current_live["recent_net_pnls"] = [float(row.get("net_pnl") or 0) for row in current_live_rows]
    current_live["closed_total"] = int(raw.get("current_live_total") or 0)
    fallback_live_rows = raw.get("fallback_live_rows") or []
    fallback_live = _stats(fallback_live_rows)
    shadow = _stats(shadow_rows)
    min_live = int(config.get("performance_guard_min_live_trades", 10))
    min_shadow = int(config.get("performance_guard_min_shadow_trades", 50))
    max_pf = float(config.get("performance_guard_max_bad_profit_factor", 0.8))
    max_win_rate = float(config.get("performance_guard_max_bad_win_rate", 25.0))
    tail_losses = 0
    for row in live_rows:
        if float(row.get("net_pnl") or 0) > 0:
            break
        tail_losses += 1
    rolling_losses = sum(1 for row in live_rows if float(row.get("net_pnl") or 0) <= 0)
    live_bad = (
        live["trades"] >= min_live
        and live["net_pnl"] < 0
        and live["profit_factor"] < max_pf
        and live["win_rate"] < max_win_rate
    )
    shadow_bad = (
        shadow["trades"] >= min_shadow
        and shadow["net_pnl"] < 0
        and shadow["profit_factor"] < max_pf
    )
    window_loss_pct = (
        abs(min(0.0, float(live.get("net_pnl") or 0))) / max(float(equity or 0), 0.00000001) * 100
        if equity
        else 0.0
    )
    soft_loss_streak = tail_losses >= int(
        config.get(
            "performance_guard_soft_consecutive_losses",
            config.get("performance_guard_severe_consecutive_losses", 4),
        )
    )
    soft_window_loss = window_loss_pct >= float(
        config.get(
            "performance_guard_soft_window_loss_equity_pct",
            config.get("performance_guard_severe_window_loss_equity_pct", 8.0),
        )
    )
    live_severe = bool(
        soft_loss_streak
        or (live["net_pnl"] < 0 and soft_window_loss)
        or (
            live["trades"] >= min_live
            and live["net_pnl"] < 0
            and (
                live["profit_factor"] < float(config.get("performance_guard_severe_profit_factor", 0.5))
                or live["win_rate"] < float(config.get("performance_guard_severe_win_rate", 20.0))
                or soft_window_loss
            )
        )
    )
    fallback_tail_losses = 0
    for row in fallback_live_rows:
        if float(row.get("net_pnl") or 0) > 0:
            break
        fallback_tail_losses += 1
    fallback_window_loss_pct = (
        abs(min(0.0, float(fallback_live.get("net_pnl") or 0))) / max(float(equity or 0), 0.00000001) * 100
        if equity
        else 0.0
    )
    fallback_live_severe = bool(
        fallback_live["trades"] >= min_live
        and fallback_live["net_pnl"] < 0
        and (
            fallback_live["profit_factor"] < float(config.get("performance_guard_severe_profit_factor", 0.5))
            or fallback_live["win_rate"] < float(config.get("performance_guard_severe_win_rate", 20.0))
            or fallback_tail_losses >= int(config.get("performance_guard_severe_consecutive_losses", 4))
            or fallback_window_loss_pct >= float(config.get("performance_guard_severe_window_loss_equity_pct", 8.0))
        )
    )
    peak_equity = float(raw.get("peak_equity") or 0)
    peak_drawdown_pct = (
        max(0.0, (peak_equity - float(equity)) / peak_equity * 100)
        if peak_equity > 0 and equity is not None
        else 0.0
    )
    soft_peak_threshold = float(
        config.get(
            "performance_guard_soft_peak_drawdown_pct",
            config.get("performance_guard_peak_drawdown_pct", 12.0),
        )
    )
    peak_drawdown_severe = peak_drawdown_pct >= soft_peak_threshold
    warmup_trades = int(config.get("performance_recovery_current_live_warmup_trades", 8))
    normal_live_pf = float(config.get("performance_recovery_normal_live_profit_factor", 1.05))
    current_live_ready = (
        int(current_live.get("closed_total") or 0) >= warmup_trades
        and current_live["net_pnl"] > 0
        and current_live["profit_factor"] >= normal_live_pf
    )
    # Superseded releases remain visible as fallback diagnostics, but cannot block a
    # newly isolated strategy family. The active release must earn or lose its own status.
    release_warmup = bool(
        release_only
        and current_family != V4_FAMILY
        and not current_live_ready
        and fallback_live_severe
    )
    hard_stop = float(config.get("hard_stop_equity", config.get("tournament_stop_equity", 5.0)))
    emergency_stop = equity is not None and float(equity) <= hard_stop
    hard_loss_streak = tail_losses >= int(config.get("performance_guard_hard_consecutive_losses", 5))
    hard_window_loss = bool(
        live["net_pnl"] < 0
        and window_loss_pct >= float(config.get("performance_guard_hard_window_loss_equity_pct", 12.0))
    )
    hard_peak_drawdown = peak_drawdown_pct >= float(
        config.get("performance_guard_hard_peak_drawdown_pct", 15.0)
    )
    hard_risk_off = bool(emergency_stop or hard_loss_streak or hard_window_loss or hard_peak_drawdown)
    release_drawdown_fallback = bool(release_equity_guard.get("fallback_active"))
    soft_risk_off = bool(
        hard_risk_off
        or live_severe
        or release_warmup
        or peak_drawdown_severe
        or (live_bad and shadow_bad)
    )
    risk_off = soft_risk_off
    latest_close_ms = int(live_rows[0].get("close_time") or 0) if live_rows else 0
    latest_close = datetime.fromtimestamp(latest_close_ms / 1000, timezone.utc) if latest_close_ms else None
    pause_minutes = float(
        config.get("performance_guard_hard_pause_minutes", 60)
        if hard_risk_off
        else config.get("performance_guard_soft_observation_minutes", 20)
    )
    # A cooldown must be anchored to an actual close. Using ``now`` when a
    # freshly released strategy has no live trades renews the deadline on every
    # guard check and permanently prevents its version-scoped canary.
    pause_until = latest_close + timedelta(minutes=pause_minutes) if risk_off and latest_close else None
    cooldown_active = bool(pause_until and now < pause_until)
    live_tail = _stats(live_rows[: max(3, min_live // 2)])
    shadow_tail = _stats(raw.get("shadow_recovery_rows") or [])
    shadow_recovery_rows = raw.get("shadow_recovery_rows") or []
    shadow_token = None
    if shadow_recovery_rows:
        newest_shadow = shadow_recovery_rows[0]
        shadow_token = f"{newest_shadow.get('id')}:{newest_shadow.get('closed_at')}"
    local_circuit = reconcile_v4_local_circuit(
        config,
        release_id=f"{current_family}@{current_version}",
        live_rows=current_live_rows,
        now=now,
    )
    continuous_active = s0_continuous_permit_active(config)
    continuous_permit = (
        s0_continuous_permit_status(config, equity=equity, live_rows=current_live_rows, now=now)
        if continuous_active
        else {}
    )
    permit = (
        {"enabled": False, "allowed": False, "status": "replaced_by_continuous_permit"}
        if continuous_active
        else recovery_permit_status(
            config,
            strategy_version=current_version,
            risk_off=risk_off,
            cooldown_active=cooldown_active,
            emergency_stop=emergency_stop,
            shadow_tail=shadow_tail,
            shadow_token=shadow_token,
            shadow_closed_total=int(raw.get("shadow_closed_total") or 0),
            current_live=current_live,
            now=now,
        )
    )
    canary = (
        {
            **continuous_permit,
            "permit_kind": "s0_continuous",
            "startup_window_active": False,
            "max_opportunities": None,
            "used_opportunities": int(current_live.get("closed_total") or 0),
        }
        if continuous_active
        else strategy_canary_status(
            config,
            active_release_id=f"{current_family}@{current_version}",
            risk_off=risk_off,
            cooldown_active=cooldown_active,
            emergency_stop=emergency_stop,
            current_live=current_live,
            eligible_shadow_rows=shadow_rows,
            now=now,
        )
    )
    canary_allowed = bool(canary.get("allowed"))
    startup_canary_active = bool(canary.get("startup_window_active"))
    recovery_allowed = bool(permit.get("allowed"))
    # During a new release's startup window, the release canary is an account-wide
    # cap for that exact version. Recovery permits must not bypass its one-position,
    # loss, opportunity, or duration budget.
    allowed = canary_allowed if startup_canary_active else recovery_allowed or canary_allowed
    recovery_level = int(permit.get("recovery_level") or 0)
    recovery_multiplier = float(permit.get("risk_multiplier") or 0.0)
    both_recovering = (
        allowed
        and current_live["trades"] >= 3
        and shadow_tail["trades"] >= recovery_shadow_limit
        and current_live["net_pnl"] > 0
        and shadow_tail["net_pnl"] > 0
        and current_live["profit_factor"] >= 1.0
        and shadow_tail["profit_factor"] >= 1.0
    )
    if both_recovering:
        recovery_level = 3
        recovery_multiplier = float(config.get("performance_guard_recovery_level_3_multiplier", 0.70))
    permit_state = str(permit.get("status") or "accumulating")
    canary_state = str(canary.get("status") or "inactive")
    status = (
        f"strategy_canary_{max(1, int(canary.get('level') or 1))}"
        if startup_canary_active and canary_allowed
        else "strategy_canary_revoked"
        if startup_canary_active and canary_state == "revoked"
        else "strategy_canary_blocked"
        if startup_canary_active
        else "normal"
        if not risk_off
        else f"strategy_canary_{max(1, int(canary.get('level') or 1))}"
        if canary_allowed
        else "recovery_3"
        if recovery_level >= 3
        else "recovery_2"
        if allowed
        else "hard_cooldown"
        if cooldown_active and hard_risk_off
        else "soft_observation"
        if cooldown_active
        else "confirming"
        if permit_state == "confirming"
        else "probe_open"
        if permit_state == "probe_open"
        else "risk_off"
    )
    release_label = active_release_version(config).upper()
    labels = {
        "normal": "正常实盘",
        "soft_observation": "局部降级观察中",
        "hard_cooldown": "全局硬冷却中",
        "risk_off": "等待影子验证恢复",
        "confirming": "恢复条件确认中",
        "probe_open": "恢复试单持仓中",
        "recovery_2": "已取得恢复试单资格",
        "recovery_3": "三级受限恢复",
        "strategy_canary_1": f"{release_label} 新策略一级试运行",
        "strategy_canary_2": f"{release_label} 新策略二级试运行",
        "strategy_canary_3": f"{release_label} 新策略已验证",
        "strategy_canary_revoked": f"{release_label} 许可证等待再签发",
        "strategy_canary_blocked": f"{release_label} 许可证暂不放行",
    }
    reason = "当前版本滚动表现正常"
    if startup_canary_active and canary_allowed:
        reason = f"{release_label} 正在执行首 24 小时限次试运行：单仓保护不变，最多验证 6 个独立机会"
    elif startup_canary_active and canary_state == "revoked":
        reason = f"{release_label} 许可证已撤销，等待当前版本合格决策影子达到再签发条件"
    elif startup_canary_active:
        reason = f"{release_label} 首日试运行暂不放行新仓：已有持仓，或机会/亏损预算已用完"
    elif canary_allowed and risk_off:
        reason = "旧版本风险背景仍保留；当前精确版本可用限次许可证验证合格候选"
    elif allowed and risk_off:
        reason = "影子恢复证据已锁定，等待首个满足成本和质量要求的候选"
    elif permit_state == "confirming":
        reason = "影子数据已达门槛，正在确认其稳定性"
    elif permit_state == "probe_open":
        reason = "恢复许可证已用于当前受保护试单，等待平仓结果"
    elif hard_risk_off:
        reason = "实盘触发硬保护，完成全局冷却后仍需当前版本合格影子证据才可小仓恢复"
    elif live_severe or release_warmup:
        reason = "实盘触发软保护；短时观察后仅放行局部证据合格的低倍率候选"
    elif peak_drawdown_severe:
        reason = f"24小时权益高点回撤 {peak_drawdown_pct:.2f}% 已触发保护"
    elif release_drawdown_fallback:
        reason = (
            f"当前版本从自身权益高点回撤 {float(release_equity_guard.get('drawdown_pct') or 0):.2f}%，"
            "已自动撤销连续质量加码与同仓追加，回到基础受限仓位"
        )
    elif live_bad and shadow_bad:
        reason = "实盘与影子交易同时处于负期望"
    elif risk_off:
        reason = "等待影子交易证明恢复后才允许小仓验证"
    if continuous_active:
        allowed = bool(continuous_permit.get("allowed"))
        risk_off = not allowed
        hard_risk_off = continuous_permit.get("status") in {"daily_paused", "hard_stop"}
        soft_risk_off = bool(allowed and float(continuous_permit.get("risk_multiplier") or 0) < 1.0)
        cooldown_active = False
        pause_until = None
        status = f"s0_continuous_{continuous_permit.get('status') or 'normal'}"
        labels[status] = {
            "s0_continuous_normal": "S0 连续准入正常",
            "s0_continuous_initial_exploration": "S0 新版本受限试探",
            "s0_continuous_position_penalty": "S0 连续准入降仓",
            "s0_continuous_daily_paused": "S0 当日回撤暂停",
            "s0_continuous_hard_stop": "账户权益硬停止",
        }.get(status, "S0 连续准入")
        reason = str(continuous_permit.get("reason") or "S0 连续准入状态已更新")
    return {
        "enabled": True,
        "allowed": allowed,
        "status": status,
        "status_label": labels[status],
        "risk_multiplier": (
            float(continuous_permit.get("risk_multiplier") or 0.0)
            if continuous_active
            else 0.0
            if not allowed
            else float(canary.get("risk_multiplier") or 0.0)
            if startup_canary_active or (risk_off and canary_allowed)
            else recovery_multiplier
            if risk_off
            else 1.0
        ),
        "pause_until": pause_until.isoformat() if pause_until else None,
        "live": live,
        "shadow": shadow,
        "live_tail": live_tail,
        "current_live": current_live,
        "fallback_live": fallback_live,
        "shadow_tail": shadow_tail,
        "recovery_level": recovery_level,
        "live_bad": live_bad,
        "shadow_bad": shadow_bad,
        "live_severe": live_severe,
        "guard_level": "hard" if hard_risk_off else "soft" if soft_risk_off else "normal",
        "soft_risk_off": soft_risk_off and not hard_risk_off,
        "hard_risk_off": hard_risk_off,
        "cooldown_active": cooldown_active,
        "cooldown_minutes": pause_minutes if risk_off else 0.0,
        "soft_thresholds": {
            "consecutive_losses": int(
                config.get(
                    "performance_guard_soft_consecutive_losses",
                    config.get("performance_guard_severe_consecutive_losses", 4),
                )
            ),
            "window_loss_equity_pct": float(
                config.get(
                    "performance_guard_soft_window_loss_equity_pct",
                    config.get("performance_guard_severe_window_loss_equity_pct", 8.0),
                )
            ),
            "peak_drawdown_pct": soft_peak_threshold,
            "observation_minutes": float(config.get("performance_guard_soft_observation_minutes", 20)),
        },
        "hard_thresholds": {
            "consecutive_losses": int(config.get("performance_guard_hard_consecutive_losses", 5)),
            "window_loss_equity_pct": float(config.get("performance_guard_hard_window_loss_equity_pct", 12.0)),
            "peak_drawdown_pct": float(config.get("performance_guard_hard_peak_drawdown_pct", 15.0)),
            "cooldown_minutes": float(config.get("performance_guard_hard_pause_minutes", 60)),
        },
        "release_warmup": release_warmup,
        "current_live_ready": current_live_ready,
        "window_loss_equity_pct": round(window_loss_pct, 4),
        "peak_equity": round(peak_equity, 8) if peak_equity else None,
        "peak_drawdown_pct": round(peak_drawdown_pct, 4),
        "peak_drawdown_severe": peak_drawdown_severe,
        "release_equity_guard": release_equity_guard,
        "release_drawdown_fallback": release_drawdown_fallback,
        "rolling_losses": rolling_losses,
        "tail_losses": tail_losses,
        "severe_loss_streak": soft_loss_streak,
        "hard_loss_streak": hard_loss_streak,
        "equity": equity,
        "active_strategy_family": current_family,
        "active_strategy_version": current_version,
        "live_evidence_scope": raw.get("live_scope"),
        "shadow_evidence_scope": raw.get("shadow_scope"),
        "recovery_permit": permit,
        "strategy_canary_permit": canary,
        "continuous_permit": continuous_permit,
        "local_circuit": local_circuit,
        "recovery_requirements": {
            "shadow_trades": recovery_shadow_limit,
            "eligible_admission_lanes": sorted(live_evidence_lanes(current_version)),
            "shadow_net_positive": True,
            "shadow_profit_factor": float(config.get("performance_recovery_entry_profit_factor", 0.9)),
            "confirm_closes": int(config.get("performance_recovery_confirm_closes", 3)),
            "confirm_minutes": float(config.get("performance_recovery_confirm_minutes", 5)),
            "permit_minutes": float(config.get("performance_recovery_permit_minutes", 180)),
            "current_live_warmup_trades": warmup_trades,
            "normal_live_profit_factor": normal_live_pf,
        },
        "reason": reason,
    }


def _evidence_rows(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    hours = float(config.get("strategy_evidence_window_hours", 24))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff_ms = int(cutoff.timestamp() * 1000)
    cutoff_iso = cutoff.isoformat()
    result: dict[str, dict[str, Any]] = {}
    with connect() as conn:
        try:
            live_rows = conn.execute(
                "SELECT symbol, direction, close_time, net_pnl, commission, funding_fee FROM live_trade_records WHERE close_time >= ?",
                (cutoff_ms,),
            ).fetchall()
        except sqlite3.OperationalError:
            live_rows = []
        try:
            shadow_rows = conn.execute(
                "SELECT symbol, direction, closed_at, net_pnl, estimated_cost FROM shadow_trades WHERE status = 'CLOSED' AND closed_at >= ?",
                (cutoff_iso,),
            ).fetchall()
        except sqlite3.OperationalError:
            shadow_rows = []
    for source, rows in (("live", live_rows), ("shadow", shadow_rows)):
        grouped: dict[str, list[dict[str, Any]]] = {}
        direction_grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            item = dict(row)
            direction = str(item.get("direction") or "").upper()
            key = f"{str(item.get('symbol') or '').upper()}:{direction}"
            grouped.setdefault(key, []).append(item)
            direction_grouped.setdefault(f"*:{direction}", []).append(item)
        for key, items in grouped.items():
            result.setdefault(key, {})[source] = _stats(items)
            if source == "live":
                latest = max(items, key=lambda item: int(item.get("close_time") or 0))
                result[key]["latest_live_close_time"] = int(latest.get("close_time") or 0)
                result[key]["latest_live_net_pnl"] = float(latest.get("net_pnl") or 0)
        for key, items in direction_grouped.items():
            result.setdefault(key, {})[source] = _stats(items)
    return result


def _release_evidence_rows(
    config: dict[str, Any],
    strategy_family: str,
    strategy_version: str,
) -> dict[str, dict[str, Any]]:
    """Load evidence for one release without mixing superseded strategy results."""
    hours = float(config.get("strategy_evidence_window_hours", 24))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff_ms = int(cutoff.timestamp() * 1000)
    cutoff_iso = cutoff.isoformat()
    result: dict[str, dict[str, Any]] = {}
    with connect() as conn:
        try:
            ensure_live_release_columns(conn)
            live_rows = conn.execute(
                "SELECT symbol, direction, close_time, net_pnl, commission, funding_fee "
                "FROM live_trade_records WHERE close_time >= ? AND strategy_family = ? "
                "AND strategy_version = ? AND strategy_role = ?",
                (cutoff_ms, strategy_family, strategy_version, ACTIVE_ROLE),
            ).fetchall()
        except sqlite3.OperationalError:
            live_rows = []
        try:
            ensure_shadow_release_columns(conn)
            shadow_rows = conn.execute(
                "SELECT symbol, direction, signal_type, closed_at, net_pnl, estimated_cost "
                "FROM shadow_trades WHERE status = 'CLOSED' AND closed_at >= ? "
                "AND strategy_family = ? AND strategy_version = ? AND strategy_role = ?",
                (cutoff_iso, strategy_family, strategy_version, ACTIVE_ROLE),
            ).fetchall()
        except sqlite3.OperationalError:
            shadow_rows = []
    for source, rows in (("live", live_rows), ("shadow", shadow_rows)):
        grouped: dict[str, list[dict[str, Any]]] = {}
        direction_grouped: dict[str, list[dict[str, Any]]] = {}
        signal_grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            item = dict(row)
            direction = str(item.get("direction") or "").upper()
            grouped.setdefault(f"{str(item.get('symbol') or '').upper()}:{direction}", []).append(item)
            direction_grouped.setdefault(f"*:{direction}", []).append(item)
            signal_type = str(item.get("signal_type") or "").lower()
            if source == "shadow" and signal_type:
                signal_grouped.setdefault(f"@signal:{signal_type}:{direction}", []).append(item)
        for key, items in grouped.items():
            result.setdefault(key, {})[source] = _stats(items)
            if source == "live":
                latest = max(items, key=lambda item: int(item.get("close_time") or 0))
                result[key]["latest_live_close_time"] = int(latest.get("close_time") or 0)
                result[key]["latest_live_net_pnl"] = float(latest.get("net_pnl") or 0)
        for key, items in direction_grouped.items():
            result.setdefault(key, {})[source] = _stats(items)
        for key, items in signal_grouped.items():
            result.setdefault(key, {})[source] = _stats(items)
    return result


def strategy_evidence_for(
    symbol: str,
    direction: str,
    config: dict[str, Any],
    *,
    strategy_family: str | None = None,
    strategy_version: str | None = None,
    signal_type: str | None = None,
) -> dict[str, Any]:
    if not config.get("strategy_evidence_enabled", True):
        return {"enabled": False, "agreement": "disabled", "risk_cap": 1.0, "boost_allowed": True}
    release_scoped = bool(strategy_family and strategy_version)
    cache_key = f"evidence_release:{strategy_family}:{strategy_version}" if release_scoped else "evidence"
    evidence_map = _cached(
        cache_key,
        float(config.get("performance_guard_cache_seconds", 15)),
        lambda: _release_evidence_rows(config, str(strategy_family), str(strategy_version))
        if release_scoped
        else _evidence_rows(config),
    )
    item = evidence_map.get(f"{symbol.upper()}:{direction.upper()}", {})
    direction_item = evidence_map.get(f"*:{direction.upper()}", {})
    live = item.get("live") or _stats([])
    shadow = item.get("shadow") or _stats([])
    live_positive = (
        live["trades"] >= int(config.get("strategy_evidence_live_confirm_trades", 8))
        and live["net_pnl"] > 0
        and live["profit_factor"] >= float(config.get("strategy_evidence_live_confirm_pf", 1.1))
    )
    shadow_positive = (
        shadow["trades"] >= int(config.get("strategy_evidence_shadow_confirm_trades", 30))
        and shadow["net_pnl"] > 0
        and shadow["profit_factor"] >= float(config.get("strategy_evidence_shadow_confirm_pf", 1.2))
    )
    live_negative = (
        live["trades"] >= int(config.get("strategy_evidence_live_negative_trades", 2))
        and live["net_pnl"] < 0
        and live["profit_factor"] < float(config.get("strategy_evidence_negative_pf", 0.8))
    )
    shadow_negative = (
        shadow["trades"] >= int(config.get("strategy_evidence_shadow_negative_trades", 10))
        and shadow["net_pnl"] < 0
        and shadow["profit_factor"] < float(config.get("strategy_evidence_negative_pf", 0.8))
    )
    if live_positive and shadow_positive:
        agreement = "positive"
        risk_cap = float(config.get("strategy_evidence_positive_risk_cap", 1.15))
    elif live_negative and shadow_negative:
        agreement = "negative"
        risk_cap = float(config.get("strategy_evidence_negative_risk_cap", 0.25))
    elif live_negative or shadow_negative:
        agreement = "weak"
        risk_cap = float(config.get("strategy_evidence_weak_risk_cap", 0.5))
    else:
        agreement = "insufficient"
        risk_cap = 1.0
    latest_close_ms = int(item.get("latest_live_close_time") or 0)
    latest_loss = float(item.get("latest_live_net_pnl") or 0) < 0
    reentry_until = None
    if latest_close_ms and latest_loss:
        reentry_until = datetime.fromtimestamp(latest_close_ms / 1000, timezone.utc) + timedelta(
            minutes=float(config.get("strategy_evidence_loss_reentry_minutes", 30))
        )
    reentry_blocked = bool(reentry_until and datetime.now(timezone.utc) < reentry_until)
    direction_live = direction_item.get("live") or _stats([])
    direction_shadow = direction_item.get("shadow") or _stats([])
    signal_item = evidence_map.get(f"@signal:{str(signal_type or '').lower()}:{direction.upper()}", {})
    signal_shadow = signal_item.get("shadow") or _stats([])
    signal_negative = (
        bool(signal_type)
        and signal_shadow["trades"] >= int(config.get("strategy_evidence_signal_negative_trades", 10))
        and signal_shadow["net_pnl"] < 0
        and signal_shadow["profit_factor"] < float(config.get("strategy_evidence_negative_pf", 0.8))
    )
    if signal_negative and agreement not in {"negative", "weak"}:
        agreement = "weak"
        risk_cap = min(risk_cap, float(config.get("strategy_evidence_weak_risk_cap", 0.5)))
    direction_negative = (
        direction_live["trades"] >= max(5, int(config.get("strategy_evidence_live_negative_trades", 2)))
        and direction_shadow["trades"] >= max(30, int(config.get("strategy_evidence_shadow_negative_trades", 10)))
        and direction_live["net_pnl"] < 0
        and direction_shadow["net_pnl"] < 0
        and direction_live["profit_factor"] < float(config.get("strategy_evidence_negative_pf", 0.8))
        and direction_shadow["profit_factor"] < float(config.get("strategy_evidence_negative_pf", 0.8))
    )
    if direction_negative:
        risk_cap = min(risk_cap, float(config.get("strategy_evidence_direction_negative_risk_cap", 0.5)))
    return {
        "enabled": True,
        "agreement": agreement,
        "agreement_label": {
            "positive": "影子与实盘共同盈利",
            "negative": "影子与实盘共同亏损",
            "weak": "一侧出现明确负向证据",
            "insufficient": "样本不足，禁止信用加仓",
        }[agreement],
        "risk_cap": round(risk_cap, 4),
        "boost_allowed": live_positive and shadow_positive and not signal_negative,
        "release_scoped": release_scoped,
        "strategy_family": strategy_family,
        "strategy_version": strategy_version,
        "signal_type": signal_type,
        "signal": {"negative": signal_negative, "shadow": signal_shadow},
        "symbol_negative": bool(live_negative or shadow_negative),
        "recovery_compatible": not (live_negative or shadow_negative or signal_negative),
        "reentry_blocked": reentry_blocked,
        "reentry_until": reentry_until.isoformat() if reentry_until else None,
        "live": live,
        "shadow": shadow,
        "direction": {
            "negative": direction_negative,
            "live": direction_live,
            "shadow": direction_shadow,
        },
    }


def apply_strategy_evidence_to_candidate(candidate: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    symbol = str(candidate.get("symbol") or "").upper()
    direction = str(candidate.get("direction") or "").upper()
    if not symbol or direction not in {"LONG", "SHORT"}:
        return candidate
    strategy_family = str(candidate.get("strategy_family") or "")
    strategy_version = str(candidate.get("strategy_version") or active_release_version(config)) if strategy_family else None
    signal = candidate.get("signal") or {}
    signal_type = str(candidate.get("entry_type") or signal.get("entry_type") or signal.get("reason") or "") or None
    evidence = strategy_evidence_for(
        symbol,
        direction,
        config,
        strategy_family=strategy_family or None,
        strategy_version=strategy_version,
        signal_type=signal_type,
    )
    if not evidence.get("enabled"):
        return candidate
    candidate = dict(candidate)
    adjustment = dict(candidate.get("live_credit_adjustment") or {})
    input_risk = float(adjustment.get("input_risk_pct") or candidate.get("risk_pct") or 0)
    cap = float(evidence.get("risk_cap") or 1.0)
    candidate["risk_pct"] = round(min(float(candidate.get("risk_pct") or 0), input_risk * cap), 8)
    if not evidence.get("boost_allowed") and adjustment:
        adjustment["risk_multiplier"] = min(float(adjustment.get("risk_multiplier") or 1.0), cap)
        adjustment["boost_qualified"] = False
        failures = list(adjustment.get("boost_failures") or [])
        if "shadow_live_not_confirmed" not in failures:
            failures.append("shadow_live_not_confirmed")
        adjustment["boost_failures"] = failures
        candidate["live_credit_adjustment"] = adjustment
    if evidence.get("reentry_blocked"):
        candidate["passed"] = False
        candidate["reason"] = "strategy_evidence_reentry_cooldown"
    message = f"证据校验：{evidence.get('agreement_label')}，风险上限 {cap:.2f}x"
    if evidence.get("reentry_blocked"):
        message += f"，亏损后同向等待至 {evidence.get('reentry_until')}"
    candidate["decision_reason"] = f"{candidate.get('decision_reason')}；{message}" if candidate.get("decision_reason") else message
    candidate["strategy_evidence"] = evidence
    return candidate


def observed_round_trip_cost_pct(config: dict[str, Any]) -> float:
    limit = int(config.get("observed_cost_window_trades", 50))

    def load() -> float:
        with connect() as conn:
            try:
                rows = conn.execute(
                    "SELECT open_notional, commission, funding_fee FROM live_trade_records WHERE open_notional > 0 ORDER BY close_time DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        values = sorted(
            (
                (abs(float(row["commission"] or 0)) + abs(float(row["funding_fee"] or 0)))
                / float(row["open_notional"])
                * 100
            )
            for row in rows
            if float(row["open_notional"] or 0) > 0
        )
        if not values:
            return float(config.get("taker_fee_pct_round_trip", 0.08)) + float(config.get("estimated_slippage_pct", 0.04))
        index = min(len(values) - 1, max(0, int(len(values) * 0.75)))
        return values[index] * float(config.get("observed_cost_safety_multiplier", 1.15))

    return float(_cached("observed_cost", float(config.get("performance_guard_cache_seconds", 15)), load))
