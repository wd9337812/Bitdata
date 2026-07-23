from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.s0_continuous_permit import s0_continuous_permit_active


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    max_notional: float = 0.0
    max_margin: float = 0.0


def equity_guard_status(
    config: dict[str, Any],
    state: dict[str, Any],
    equity: float | None,
    mode: str | None = None,
) -> dict[str, Any]:
    if not config.get("equity_guard_enabled", True):
        return {"enabled": False, "allowed": True, "risk_multiplier": 1.0, "drawdown_pct": 0.0}
    if equity is None:
        return {"enabled": True, "allowed": False, "risk_multiplier": 0.0, "drawdown_pct": 0.0, "reason": "account_unavailable"}
    baseline_mode = "global"
    high_watermark_key = "equity_high_watermark"
    if mode in {"extreme_sprint", "yolo_scalp"}:
        if (
            config.get("equity_guard_release_baseline_enabled", True)
            and state.get("strategy_release_equity_id")
            and float(state.get("strategy_release_equity_high_watermark") or 0) > 0
        ):
            baseline_mode = str(state.get("strategy_release_equity_id"))
            high_watermark_key = "strategy_release_equity_high_watermark"
        else:
            baseline_mode = str(mode)
            high_watermark_key = "extreme_sprint_equity_high_watermark"
    high_watermark = max(float(state.get(high_watermark_key) or equity), float(equity))
    drawdown_pct = max(0.0, (high_watermark - float(equity)) / high_watermark * 100) if high_watermark > 0 else 0.0
    pause_key = "yolo_scalp_equity_guard_pause_drawdown_pct" if mode == "yolo_scalp" else "extreme_equity_guard_pause_drawdown_pct" if mode == "extreme_sprint" else "equity_guard_pause_drawdown_pct"
    v44_full_bet = bool(
        mode == "extreme_sprint"
        and str(config.get("opportunity_v4_strategy_version") or "").lower().startswith(("v4.4", "v4.5", "v4.6", "v4.7", "v4.8", "v4.9", "v4.10", "v4.11"))
        and config.get("opportunity_v44_full_bet_enabled", True)
    )
    pause_pct = float(
        config.get("opportunity_v44_release_pause_drawdown_pct", 35.0)
        if v44_full_bet
        else config.get(pause_key, config.get("equity_guard_pause_drawdown_pct", 35.0))
    )
    if drawdown_pct >= pause_pct:
        return {
            "enabled": True,
            "allowed": False,
            "risk_multiplier": 0.0,
            "drawdown_pct": round(drawdown_pct, 4),
            "reason": "equity_guard_pause",
            "baseline_mode": baseline_mode,
            "high_watermark": round(high_watermark, 8),
        }
    if v44_full_bet:
        return {
            "enabled": True,
            "allowed": True,
            "risk_multiplier": 1.0,
            "drawdown_pct": round(drawdown_pct, 4),
            "reason": "v44_full_bet_until_hard_pause",
            "baseline_mode": baseline_mode,
            "high_watermark": round(high_watermark, 8),
        }
    multiplier = 1.0
    levels = [
        (float(config.get("equity_guard_drawdown_3_pct", 25.0)), float(config.get("equity_guard_multiplier_3", 0.2))),
        (float(config.get("equity_guard_drawdown_2_pct", 18.0)), float(config.get("equity_guard_multiplier_2", 0.45))),
        (float(config.get("equity_guard_drawdown_1_pct", 10.0)), float(config.get("equity_guard_multiplier_1", 0.75))),
    ]
    for threshold, value in levels:
        if drawdown_pct >= threshold:
            multiplier = value
            break
    return {
        "enabled": True,
        "allowed": True,
        "risk_multiplier": multiplier,
        "drawdown_pct": round(drawdown_pct, 4),
        "reason": "scaled" if multiplier < 1 else "ok",
        "baseline_mode": baseline_mode,
        "high_watermark": round(high_watermark, 8),
    }


def direction_cooldown_key(symbol: str, direction: str | None) -> str:
    direction = (direction or "").upper()
    return f"{symbol.upper()}:{direction}" if direction in {"LONG", "SHORT"} else symbol.upper()


def _cooldown_active(until: str | None) -> tuple[bool, str | None]:
    if not until:
        return False, None
    try:
        return datetime.fromisoformat(until) > datetime.now(timezone.utc), None
    except ValueError:
        return False, "invalid_cooldown_state"


def live_trading_allowed(config: dict[str, Any]) -> bool:
    return (
        not config.get("dry_run", True)
        and config.get("live_trading_enabled") is True
        and config.get("live_trading_confirmation") == "ENABLE_LIVE_TRADING"
    )


def current_stage(config: dict[str, Any], state: dict[str, Any], equity: float | None) -> str:
    saved_stage = state.get("stage", "growth")
    if equity is None:
        return saved_stage
    if saved_stage == "grid":
        return "grid"
    if equity >= float(config.get("stage1_target_equity", 10000)) and config.get("stage2_activation") == "auto":
        return "grid"
    return "growth"


def assess_new_position(
    config: dict[str, Any],
    state: dict[str, Any],
    equity: float,
    symbol: str,
    open_positions: list[dict[str, Any]],
    overrides: dict[str, Any] | None = None,
) -> RiskDecision:
    overrides = overrides or {}
    direction = str(overrides.get("direction") or "").upper()
    if state.get("bot_status") != "running":
        return RiskDecision(False, "bot_paused")
    hard_stop = float(config.get("hard_stop_equity", config.get("tournament_stop_equity", 5.0)))
    if hard_stop > 0 and equity <= hard_stop:
        return RiskDecision(False, "hard_stop_equity")

    continuous_s0 = s0_continuous_permit_active(config)
    if not continuous_s0:
        cooldown_until = state.get("cooldown_until")
        active, error = _cooldown_active(cooldown_until)
        if error:
            return RiskDecision(False, error)
        if active:
            return RiskDecision(False, "cooldown_active")

    if not continuous_s0 and config.get("directional_cooldown_enabled", True) and direction in {"LONG", "SHORT"}:
        direction_cooldowns = state.get("symbol_direction_cooldowns") or {}
        direction_until = (
            direction_cooldowns.get(direction_cooldown_key(symbol, direction))
            if isinstance(direction_cooldowns, dict)
            else None
        )
        active, error = _cooldown_active(direction_until)
        if error:
            return RiskDecision(False, "invalid_direction_cooldown_state")
        if active and not overrides.get("ignore_direction_cooldown", False):
            return RiskDecision(False, "symbol_direction_cooldown_active")

    if not continuous_s0:
        symbol_cooldowns = state.get("symbol_cooldowns") or {}
        symbol_cooldown_until = symbol_cooldowns.get(symbol.upper()) if isinstance(symbol_cooldowns, dict) else None
        active, error = _cooldown_active(symbol_cooldown_until)
        if error:
            return RiskDecision(False, "invalid_symbol_cooldown_state")
        if active and config.get("legacy_symbol_cooldown_blocks", False):
            return RiskDecision(False, "symbol_cooldown_active")

    max_consecutive_losses = int(overrides.get("max_consecutive_losses", config.get("max_consecutive_losses", 2)))
    if not continuous_s0 and int(state.get("consecutive_losses", 0)) >= max_consecutive_losses:
        return RiskDecision(False, "consecutive_loss_limit")

    daily_start = float(state.get("daily_start_equity") or equity)
    daily_loss_limit_pct = float(
        config.get("s0_continuous_daily_pause_pct", 30.0)
        if continuous_s0
        else overrides.get("daily_loss_limit_pct", config.get("daily_loss_limit_pct", 3.0))
    )
    if daily_start > 0:
        daily_dd_pct = max(0.0, (daily_start - equity) / daily_start * 100)
        if daily_dd_pct >= daily_loss_limit_pct:
            return RiskDecision(False, "daily_loss_limit")

    if not continuous_s0 and not overrides.get("ignore_max_drawdown", False):
        high_watermark = max(float(state.get("equity_high_watermark") or equity), equity)
        if high_watermark > 0:
            drawdown_pct = max(0.0, (high_watermark - equity) / high_watermark * 100)
            if drawdown_pct >= float(config.get("max_drawdown_pct", 15.0)):
                return RiskDecision(False, "max_drawdown_limit")

    active_positions = [
        pos for pos in open_positions
        if abs(float(pos.get("positionAmt", pos.get("amount", 0)))) > 0
    ]
    max_open_positions = int(overrides.get("max_open_positions", config.get("max_open_positions", 1)))
    if len(active_positions) >= max_open_positions and not overrides.get("ignore_max_open_positions", False):
        return RiskDecision(False, "max_open_positions")

    symbol_margin_pct = float(overrides.get("margin_pct", config.get("max_symbol_margin_pct", 35.0)))
    max_margin = equity * symbol_margin_pct / 100
    leverage = float(overrides.get("leverage", config.get("stage1_max_leverage", 2)))
    return RiskDecision(True, "allowed", max_notional=max_margin * leverage, max_margin=max_margin)


def position_size_from_risk(equity: float, risk_pct: float, entry: float, stop: float) -> float:
    risk_amount = equity * risk_pct / 100
    per_unit_risk = abs(entry - stop)
    if per_unit_risk <= 0:
        return 0.0
    return risk_amount / per_unit_risk
