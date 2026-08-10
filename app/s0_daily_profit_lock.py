from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.telemetry import connect
from app.state_store import save_state


def _daily_realized_net(now: datetime) -> tuple[float, int]:
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    cutoff_ms = int(day_start.timestamp() * 1000)
    try:
        with connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'live_trade_records'"
            ).fetchone()
            if not exists:
                return 0.0, 0
            row = conn.execute(
                """
                SELECT COALESCE(SUM(net_pnl), 0) AS net_pnl, COUNT(*) AS closed_trades
                FROM live_trade_records
                WHERE close_time >= ?
                """,
                (cutoff_ms,),
            ).fetchone()
            return float(row["net_pnl"] or 0.0), int(row["closed_trades"] or 0)
    except Exception:
        return 0.0, 0


def s0_daily_profit_lock_status(
    config: dict[str, Any],
    state: dict[str, Any],
    account: dict[str, Any],
    *,
    now: datetime | None = None,
    realized_net: float | None = None,
    closed_trades: int | None = None,
) -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    route = config.get("_stage_route") or state.get("stage_route") or {}
    is_s0 = str(route.get("stage") or state.get("active_stage") or "") == "S0"
    enabled = bool(config.get("stage_s0_daily_profit_lock_enabled", True)) and is_s0
    target_pct = float(config.get("stage_s0_daily_profit_target_pct", 50.0))
    daily_start = float(state.get("daily_start_equity") or account.get("equity") or 0.0)
    target_usdt = max(0.0, daily_start * target_pct / 100.0)
    if realized_net is None or closed_trades is None:
        queried_net, queried_trades = _daily_realized_net(now)
        realized_net = queried_net if realized_net is None else realized_net
        closed_trades = queried_trades if closed_trades is None else closed_trades
    realized_net = float(realized_net or 0.0)
    closed_trades = int(closed_trades or 0)
    positions = [
        item
        for item in account.get("positions", [])
        if abs(float(item.get("positionAmt", item.get("amount", 0)) or 0)) > 0
    ]
    session_date = now.date().isoformat()
    already_locked = bool(
        state.get("s0_daily_profit_lock_active")
        and state.get("s0_daily_profit_lock_date") == session_date
    )
    target_reached = enabled and target_usdt > 0 and realized_net >= target_usdt
    pending = target_reached and bool(positions) and not already_locked
    active = already_locked or (target_reached and not positions)
    progress_pct = (
        max(0.0, realized_net) / target_usdt * 100.0
        if target_usdt > 0
        else 0.0
    )
    status = {
        "enabled": enabled,
        "active": active,
        "pending_flat": pending,
        "blocks_new_entries": active,
        "session_date": session_date,
        "daily_start_equity": round(daily_start, 8),
        "target_pct": round(target_pct, 4),
        "target_usdt": round(target_usdt, 8),
        "lock_equity": round(daily_start + target_usdt, 8),
        "current_equity": round(float(account.get("equity") or 0.0), 8),
        "remaining_profit_usdt": round(max(0.0, target_usdt - realized_net), 8),
        "realized_net_pnl": round(realized_net, 8),
        "closed_trades": closed_trades,
        "progress_pct": round(progress_pct, 4),
        "reset_at": (
            datetime(now.year, now.month, now.day, tzinfo=timezone.utc) + timedelta(days=1)
        ).isoformat(),
        "reason": (
            "daily_profit_locked"
            if active
            else "target_reached_waiting_flat"
            if pending
            else "target_not_reached"
        ),
    }
    updates = {
        "daily_start_equity": daily_start,
        "daily_realized_pnl": round(realized_net, 8),
        "s0_daily_profit_lock_active": active,
        "s0_daily_profit_lock_pending": pending,
        "s0_daily_profit_lock_date": session_date if active or pending else None,
        "s0_daily_profit_lock_triggered_at": (
            state.get("s0_daily_profit_lock_triggered_at") or now.isoformat()
            if active
            else None
        ),
        "s0_daily_profit_lock_status": status,
    }
    save_state(updates)
    return status
