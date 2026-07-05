from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    max_notional: float = 0.0
    max_margin: float = 0.0


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
    if equity <= float(config.get("tournament_stop_equity", 0)):
        return RiskDecision(False, "tournament_stop_equity")

    cooldown_until = state.get("cooldown_until")
    active, error = _cooldown_active(cooldown_until)
    if error:
        return RiskDecision(False, error)
    if active:
        return RiskDecision(False, "cooldown_active")

    if config.get("directional_cooldown_enabled", True) and direction in {"LONG", "SHORT"}:
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

    symbol_cooldowns = state.get("symbol_cooldowns") or {}
    symbol_cooldown_until = symbol_cooldowns.get(symbol.upper()) if isinstance(symbol_cooldowns, dict) else None
    active, error = _cooldown_active(symbol_cooldown_until)
    if error:
        return RiskDecision(False, "invalid_symbol_cooldown_state")
    if active and config.get("legacy_symbol_cooldown_blocks", False):
        return RiskDecision(False, "symbol_cooldown_active")

    max_consecutive_losses = int(overrides.get("max_consecutive_losses", config.get("max_consecutive_losses", 2)))
    if int(state.get("consecutive_losses", 0)) >= max_consecutive_losses:
        return RiskDecision(False, "consecutive_loss_limit")

    daily_start = float(state.get("daily_start_equity") or equity)
    daily_loss_limit_pct = float(overrides.get("daily_loss_limit_pct", config.get("daily_loss_limit_pct", 3.0)))
    if daily_start > 0:
        daily_dd_pct = max(0.0, (daily_start - equity) / daily_start * 100)
        if daily_dd_pct >= daily_loss_limit_pct:
            return RiskDecision(False, "daily_loss_limit")

    if not overrides.get("ignore_max_drawdown", False):
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
