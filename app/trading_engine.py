from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.binance_client import BinanceFuturesClient
from app.exchange_filters import ExchangeFilters
from app.grid import build_grid_orders, build_grid_plan
from app.position_sizing import (
    effective_order_viability,
    effective_position_risk,
    explain_position_sizing,
    extreme_scalp_tier,
    unified_position_sizing,
)
from app.protection_audit import audit_position_protection, enrich_positions_with_prices
from app.protection import apply_initial_protection_to_signal, build_protection_plan
from app.risk import assess_new_position, current_stage, equity_guard_status, live_trading_allowed, position_size_from_risk
from app.scanner import latest_strategy_signal, mode_config, scan_growth_candidates, strategy_params_for_mode
from app.state_store import save_state
from app.strategy import StrategyParams
from app.target import target_progress, target_state_updates


def position_direction(position: dict[str, Any]) -> str:
    direction = str(position.get("direction") or "").upper()
    if direction in {"LONG", "SHORT"}:
        return direction
    side = str(position.get("positionSide") or "").upper()
    if side in {"LONG", "SHORT"}:
        return side
    amount = float(position.get("positionAmt", position.get("amount", 0)) or 0)
    return "SHORT" if amount < 0 else "LONG"


def position_symbol(position: dict[str, Any]) -> str:
    return str(position.get("symbol") or "").upper()


def position_amount_abs(position: dict[str, Any]) -> float:
    return abs(float(position.get("positionAmt", position.get("amount", position.get("quantity", 0))) or 0))


def is_reduce_only_rejection(exc: Exception) -> bool:
    text = str(exc)
    return "-2022" in text or "ReduceOnly Order is rejected" in text


def find_live_position(
    client: BinanceFuturesClient,
    symbol: str,
    direction: str,
) -> dict[str, Any] | None:
    if not hasattr(client, "account_live"):
        return None
    account = summarize_account(client.account_live())
    symbol = symbol.upper()
    direction = direction.upper()
    for position in account.get("positions", []):
        if position_symbol(position) == symbol and position_direction(position) == direction:
            if position_amount_abs(position) > 0:
                return position
    return None


def position_margin(position: dict[str, Any]) -> float:
    margin = float(position.get("positionInitialMargin", 0) or 0)
    if margin > 0:
        return margin
    notional = abs(float(position.get("notional", 0) or 0))
    leverage = max(1.0, float(position.get("leverage", 1) or 1))
    return notional / leverage if notional > 0 else 0.0


def position_pnl_pct(position: dict[str, Any]) -> float:
    margin = position_margin(position)
    if margin <= 0:
        return 0.0
    return float(position.get("unrealizedProfit", 0) or 0) / margin * 100


def candidate_score(candidate: dict[str, Any] | None) -> float:
    if not candidate:
        return 0.0
    return float(candidate.get("score") or 0)


def find_position_scan_candidate(position: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    symbol = position_symbol(position)
    direction = position_direction(position)
    for candidate in candidates:
        if str(candidate.get("symbol") or "").upper() == symbol and str(candidate.get("direction") or "").upper() == direction:
            return candidate
    return None


def rotation_candidate_type(candidate: dict[str, Any] | None) -> str:
    if not candidate:
        return "unknown"
    entry_type = str(candidate.get("entry_type") or "").lower()
    if entry_type in {"extreme_probe", "weak_quality_probe", "preemptive", "momentum", "small_standard", "observe_standard"}:
        return entry_type
    return "standard"


def rotation_required_delta(config: dict[str, Any], mode: str, current_pnl_pct: float, current_type: str) -> float:
    base = float(config.get(f"{mode}_rotation_min_score_delta", config.get("rotation_min_score_delta", 12.0)))
    if current_type in {"extreme_probe", "weak_quality_probe", "preemptive", "momentum", "small_standard", "observe_standard"}:
        return min(base, float(config.get("rotation_probe_min_score_delta", 8.0)))
    if current_pnl_pct >= 0:
        return max(base, float(config.get("rotation_profit_min_score_delta", 25.0)))
    if abs(current_pnl_pct) >= float(config.get("rotation_small_loss_pct", 0.5)):
        return min(base, float(config.get("rotation_loss_min_score_delta", 8.0)))
    return base


def rotation_cost_metrics(candidate: dict[str, Any], config: dict[str, Any]) -> dict[str, float]:
    expected_profit_pct = float(candidate.get("expected_profit_pct") or 0.0)
    fallback_cost_ratio = float(candidate.get("cost_ratio") or 0.0)
    estimated_cost_pct = float(candidate.get("estimated_cost_pct") or 0.0)
    if expected_profit_pct <= 0 and fallback_cost_ratio > 0:
        return {
            "expected_profit_pct": 0.0,
            "estimated_new_trade_cost_pct": 0.0,
            "extra_close_cost_pct": round(float(config.get("rotation_extra_close_cost_pct", 0.08)), 6),
            "total_rotation_cost_pct": 0.0,
            "rotation_cost_ratio": round(fallback_cost_ratio, 6),
        }
    if estimated_cost_pct <= 0:
        if fallback_cost_ratio > 0:
            estimated_cost_pct = expected_profit_pct / fallback_cost_ratio if expected_profit_pct > 0 else 0.0
    if estimated_cost_pct <= 0:
        estimated_cost_pct = float(config.get("estimated_slippage_pct", 0.04)) + 0.08
    extra_close_cost_pct = float(config.get("rotation_extra_close_cost_pct", 0.08))
    total_cost_pct = estimated_cost_pct + extra_close_cost_pct
    rotation_cost_ratio = expected_profit_pct / total_cost_pct if total_cost_pct > 0 else 0.0
    return {
        "expected_profit_pct": round(expected_profit_pct, 6),
        "estimated_new_trade_cost_pct": round(estimated_cost_pct, 6),
        "extra_close_cost_pct": round(extra_close_cost_pct, 6),
        "total_rotation_cost_pct": round(total_cost_pct, 6),
        "rotation_cost_ratio": round(rotation_cost_ratio, 6),
    }


def rotation_cooldown_active(state: dict[str, Any], symbol: str) -> bool:
    cooldowns = state.get("rotation_cooldowns") or {}
    until = cooldowns.get(symbol.upper()) if isinstance(cooldowns, dict) else None
    if not until:
        return False
    try:
        return datetime.fromisoformat(until) > datetime.now(timezone.utc)
    except ValueError:
        return False


def build_position_rotation_plan(
    candidate: dict[str, Any],
    scan: dict[str, Any],
    config: dict[str, Any],
    state: dict[str, Any],
    account_summary: dict[str, Any],
) -> dict[str, Any]:
    mode = str((scan.get("mode") or {}).get("mode") or config.get("growth_mode") or "balanced")
    if not config.get("position_rotation_enabled", True):
        return {"allowed": False, "reason": "rotation_disabled"}
    if not config.get(f"{mode}_rotation_enabled", False):
        return {"allowed": False, "reason": f"{mode}_rotation_disabled"}

    positions = [
        position for position in account_summary.get("positions", [])
        if position_amount_abs(position) > 0
    ]
    if not positions:
        return {"allowed": False, "reason": "no_position_to_rotate"}

    new_symbol = str(candidate.get("symbol") or "").upper()
    new_direction = str(candidate.get("direction") or "").upper()
    if any(position_symbol(position) == new_symbol and position_direction(position) == new_direction for position in positions):
        return {"allowed": False, "reason": "same_position_already_open"}
    if rotation_cooldown_active(state, new_symbol):
        return {"allowed": False, "reason": "rotation_cooldown_active"}

    candidates = list(scan.get("candidates") or [])
    unknown_score = float(config.get("rotation_unknown_position_score", 75.0))
    scored_positions = []
    for position in positions:
        current_candidate = find_position_scan_candidate(position, candidates)
        current_score = candidate_score(current_candidate) if current_candidate else unknown_score
        scored_positions.append(
            {
                "position": position,
                "score": current_score,
                "pnl_pct": position_pnl_pct(position),
                "scan_candidate": current_candidate,
            }
        )
    weakest = min(scored_positions, key=lambda item: (item["score"], item["pnl_pct"]))
    new_score = candidate_score(candidate)
    min_new_score = float(config.get(f"{mode}_rotation_min_new_score", 999.0))
    configured_min_delta = float(config.get(f"{mode}_rotation_min_score_delta", 999.0))
    min_cost_ratio = float(config.get("rotation_min_cost_ratio", 8.0))
    min_net_cost_ratio = float(config.get("rotation_min_net_cost_ratio", 2.5))
    keep_winner_profit_pct = float(
        config.get("rotation_keep_winner_profit_pct_extreme", config.get("rotation_keep_winner_profit_pct", 3.0))
        if mode == "extreme_sprint"
        else config.get("rotation_keep_winner_profit_pct", 3.0)
    )
    max_current_loss_pct = float(config.get("rotation_max_current_loss_pct", 6.0))
    cost_ratio = float(candidate.get("cost_ratio") or 0)
    score_delta = new_score - float(weakest["score"])
    current_pnl_pct = float(weakest["pnl_pct"])
    current_type = rotation_candidate_type(weakest.get("scan_candidate"))
    min_delta = rotation_required_delta(config, mode, current_pnl_pct, current_type)
    cost_metrics = rotation_cost_metrics(candidate, config)

    if new_score < min_new_score:
        reason = "new_score_below_rotation_threshold"
    elif score_delta < min_delta:
        reason = "score_delta_too_small"
    elif cost_ratio < min_cost_ratio:
        reason = "cost_ratio_too_low"
    elif cost_metrics["rotation_cost_ratio"] < min_net_cost_ratio:
        reason = "rotation_cost_ratio_too_low"
    elif current_pnl_pct >= keep_winner_profit_pct:
        reason = "current_position_is_winner"
    elif current_pnl_pct <= -max_current_loss_pct:
        reason = "current_position_near_hard_loss"
    else:
        reason = "rotation_allowed"

    return {
        "allowed": reason == "rotation_allowed",
        "reason": reason,
        "mode": mode,
        "from": {
            "symbol": position_symbol(weakest["position"]),
            "direction": position_direction(weakest["position"]),
            "quantity": position_amount_abs(weakest["position"]),
            "score": weakest["score"],
            "entry_type": current_type,
            "pnl_pct_on_margin": current_pnl_pct,
            "unrealized_pnl": float(weakest["position"].get("unrealizedProfit", 0) or 0),
        },
        "to": {
            "symbol": new_symbol,
            "direction": new_direction,
            "score": new_score,
            "cost_ratio": cost_ratio,
            "entry_type": rotation_candidate_type(candidate),
        },
        "thresholds": {
            "min_new_score": min_new_score,
            "configured_min_score_delta": configured_min_delta,
            "effective_min_score_delta": min_delta,
            "min_cost_ratio": min_cost_ratio,
            "min_net_cost_ratio": min_net_cost_ratio,
            "keep_winner_profit_pct": keep_winner_profit_pct,
            "max_current_loss_pct": max_current_loss_pct,
        },
        "score_delta": score_delta,
        "costs": cost_metrics,
    }


def summarize_account(account: dict[str, Any] | None) -> dict[str, Any]:
    if not account:
        return {"equity": None, "available_balance": None, "unrealized_pnl": None, "positions": []}
    positions = [
        pos for pos in account.get("positions", [])
        if abs(float(pos.get("positionAmt", 0))) > 0
    ]
    return {
        "equity": float(account.get("totalWalletBalance", 0)) + float(account.get("totalUnrealizedProfit", 0)),
        "available_balance": float(account.get("availableBalance", 0)),
        "unrealized_pnl": float(account.get("totalUnrealizedProfit", 0)),
        "positions": positions,
    }


def sync_stage(config: dict[str, Any], state: dict[str, Any], account_summary: dict[str, Any]) -> dict[str, Any]:
    equity = account_summary.get("equity")
    updates: dict[str, Any] = {}
    if equity is not None:
        updates["equity_high_watermark"] = max(float(state.get("equity_high_watermark") or 0), float(equity))
        if not state.get("daily_start_equity"):
            updates["daily_start_equity"] = float(equity)
    stage = current_stage(config, state, equity)
    updates["stage"] = stage
    updates.update(target_state_updates(config, state, account_summary))
    active_mode = mode_config(config, equity).get("mode")
    if equity is not None and active_mode == "extreme_sprint":
        existing_mode = state.get("equity_guard_mode")
        current_extreme_high = float(state.get("extreme_sprint_equity_high_watermark") or 0)
        if existing_mode != "extreme_sprint" or current_extreme_high <= 0:
            updates["extreme_sprint_start_equity"] = float(equity)
            updates["extreme_sprint_equity_high_watermark"] = float(equity)
        else:
            updates["extreme_sprint_equity_high_watermark"] = max(current_extreme_high, float(equity))
        updates["equity_guard_mode"] = "extreme_sprint"
    elif active_mode:
        updates["equity_guard_mode"] = active_mode
    return save_state(updates) if updates else state


def build_stage1_decision(
    symbol: str,
    bars: list[list[Any]],
    config: dict[str, Any],
    state: dict[str, Any],
    account_summary: dict[str, Any],
    scan_candidate: dict[str, Any] | None = None,
    risk_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    active_mode = mode_config(config, account_summary.get("equity"))
    if scan_candidate:
        active_mode = {
            **active_mode,
            "mode": scan_candidate.get("mode", active_mode["mode"]),
            "strategy": scan_candidate.get("strategy", active_mode["strategy"]),
            "risk_pct": scan_candidate.get("risk_pct", active_mode["risk_pct"]),
            "leverage": scan_candidate.get("leverage", active_mode["leverage"]),
            "margin_pct": scan_candidate.get("margin_pct", active_mode["margin_pct"]),
        }
    direction = str((scan_candidate or {}).get("direction", "LONG")).upper()
    candidate_signal = (scan_candidate or {}).get("signal") or {}
    if candidate_signal.get("signal") == direction and candidate_signal.get("stop") and candidate_signal.get("take_profit"):
        signal = candidate_signal
    else:
        signal_params = strategy_params_for_mode(config, active_mode, "standard") or StrategyParams()
        signal = latest_strategy_signal(symbol, bars, active_mode["strategy"], signal_params, direction=direction)
    equity = account_summary.get("equity")
    if signal.get("signal") not in {"LONG", "SHORT"} or signal.get("signal") != direction:
        return {"symbol": symbol, "action": "WAIT", "signal": signal, "risk": {"allowed": False, "reason": "no_signal"}}
    if equity is None:
        return {"symbol": symbol, "action": "WAIT", "signal": signal, "risk": {"allowed": False, "reason": "account_unavailable"}}
    entry_type = (scan_candidate or {}).get("entry_type", signal.get("entry_type", "standard"))
    scalp_tier = extreme_scalp_tier(scan_candidate, config)
    if scalp_tier != "none":
        signal = dict(signal)
        signal["entry_type"] = "extreme_scalp"
        signal["entry_type_label"] = "极限短打"
        signal["protection_profile"] = {
            "stop_atr": float(config.get("extreme_scalp_stop_atr", 0.55)),
            "take_profit_atr": float(config.get("extreme_scalp_take_profit_atr", 0.75)),
            "max_hold_bars": int(config.get("extreme_scalp_max_hold_bars", 2)),
        }
        entry_type = "extreme_scalp"
    protection_plan = build_protection_plan(signal, config, entry_type=entry_type, direction=direction)
    signal = apply_initial_protection_to_signal(signal, protection_plan)
    guard = equity_guard_status(config, state, equity, active_mode["mode"])
    target = target_progress(config, state, account_summary)
    if not guard.get("allowed", True):
        return {
            "symbol": symbol,
            "action": "WAIT",
            "signal": signal,
            "risk": {"allowed": False, "reason": guard.get("reason", "equity_guard")},
            "equity_guard": guard,
            "target_progress": target,
            "protection_plan": protection_plan,
            "mode": active_mode["mode"],
            "strategy": active_mode["strategy"],
        }
    effective_risk = effective_position_risk(
        candidate_risk_pct=float(active_mode["risk_pct"]),
        candidate=scan_candidate,
        guard=guard,
        target=target,
        config=config,
        mode=str(active_mode["mode"]),
    )
    active_mode["risk_pct"] = float(effective_risk["final_risk_pct"])

    daily_loss_key = "daily_loss_limit_pct"
    if active_mode["mode"] == "attack":
        daily_loss_key = "attack_daily_loss_limit_pct"
    if active_mode["mode"] == "tournament":
        daily_loss_key = "tournament_daily_loss_limit_pct"
    if active_mode["mode"] == "tournament_sprint":
        daily_loss_key = "tournament_sprint_daily_loss_limit_pct"
    if active_mode["mode"] == "extreme_sprint":
        daily_loss_key = "extreme_sprint_daily_loss_limit_pct"
    max_open_positions = config.get("max_open_positions", 1)
    if active_mode["mode"] in {"tournament_sprint", "extreme_sprint"}:
        max_open_positions = int(config.get("tournament_sprint_max_open_positions", max_open_positions))
        if equity is not None and float(equity) < float(config.get("tournament_sprint_second_position_equity", 100.0)):
            max_open_positions = min(max_open_positions, 1)
        if active_mode["mode"] == "extreme_sprint":
            max_open_positions = int(config.get("extreme_sprint_max_open_positions", max_open_positions))
            if float(equity) < float(config.get("tournament_sprint_second_position_equity", 100.0)):
                max_open_positions = min(max_open_positions, 1)
    overrides = {
        "direction": direction,
        "margin_pct": active_mode["margin_pct"],
        "leverage": active_mode["leverage"],
        "daily_loss_limit_pct": config.get(daily_loss_key, config.get("daily_loss_limit_pct", 3.0)),
        "ignore_max_drawdown": active_mode["mode"] in {"tournament", "tournament_sprint", "extreme_sprint"},
        "max_open_positions": max_open_positions,
        "max_consecutive_losses": (
            config.get("extreme_sprint_max_consecutive_losses", config.get("max_consecutive_losses", 2))
            if active_mode["mode"] == "extreme_sprint"
            else
            config.get("tournament_sprint_max_consecutive_losses", config.get("max_consecutive_losses", 2))
            if active_mode["mode"] == "tournament_sprint"
            else config.get("max_consecutive_losses", 2)
        ),
    }
    overrides.update(risk_overrides or {})
    risk = assess_new_position(
        config,
        state,
        equity,
        symbol,
        account_summary.get("positions", []),
        overrides=overrides,
    )
    quantity = position_size_from_risk(
        equity=equity,
        risk_pct=float(active_mode["risk_pct"]),
        entry=float(signal["last_price"]),
        stop=float(signal["stop"]),
    )
    max_qty = risk.max_notional / float(signal["last_price"]) if signal.get("last_price") else 0
    quantity = min(quantity, max_qty)
    estimated_notional = quantity * float(signal["last_price"])
    order_viability = effective_order_viability(
        notional=estimated_notional,
        candidate=scan_candidate,
        config=config,
    )
    if config.get("effective_position_sizing_enabled", True) and not order_viability["allowed"]:
        risk_dict = risk.__dict__
        return {
            "symbol": symbol,
            "action": "WAIT",
            "direction": direction,
            "signal": signal,
            "risk": {**risk_dict, "allowed": False, "reason": "ineffective_order"},
            "quantity": quantity,
            "estimated_notional": estimated_notional,
            "effective_risk": effective_risk,
            "order_viability": order_viability,
            "mode": active_mode["mode"],
            "strategy": active_mode["strategy"],
            "entry_type": entry_type,
            "decision_reason": "订单预期净收益不足以覆盖交易成本和噪声",
            "equity_guard": guard,
            "target_progress": target,
            "protection_plan": protection_plan,
        }
    risk_dict = risk.__dict__
    sizing = explain_position_sizing(
        base_risk_pct=float((scan_candidate or {}).get("base_risk_pct") or (scan_candidate or {}).get("risk_pct") or active_mode["risk_pct"]),
        candidate=scan_candidate,
        guard=guard,
        target=target,
        final_risk_pct=float(active_mode["risk_pct"]),
        risk=risk_dict,
    )
    unified_sizing = unified_position_sizing(
        stage_base_risk=float((scan_candidate or {}).get("base_risk_pct") or (scan_candidate or {}).get("risk_pct") or active_mode["risk_pct"]),
        candidate=scan_candidate,
        guard=guard,
        target=target,
        risk_caps={
            "max_notional": risk_dict.get("max_notional"),
            "max_margin": risk_dict.get("max_margin"),
            "max_risk_pct": active_mode["risk_pct"],
        },
    )
    return {
        "symbol": symbol,
        "action": f"OPEN_{direction}" if risk.allowed and quantity > 0 else "WAIT",
        "direction": direction,
        "signal": signal,
        "risk": risk_dict,
        "position_sizing": sizing,
        "unified_position_sizing": unified_sizing,
        "quantity": quantity,
        "estimated_notional": quantity * float(signal["last_price"]),
        "effective_risk": effective_risk,
        "order_viability": order_viability,
        "mode": active_mode["mode"],
        "strategy": active_mode["strategy"],
        "entry_type": entry_type,
        "decision_reason": (scan_candidate or {}).get("decision_reason"),
        "equity_guard": guard,
        "target_progress": target,
        "protection_plan": protection_plan,
        "scalp_tier": scalp_tier,
        "risk_pct": active_mode["risk_pct"],
        "leverage": active_mode["leverage"],
    }


def build_best_growth_decision(
    client: BinanceFuturesClient,
    config: dict[str, Any],
    state: dict[str, Any],
    account_summary: dict[str, Any],
    symbols_override: list[str] | None = None,
    fast_lane: bool = False,
) -> dict[str, Any]:
    scan = scan_growth_candidates(
        client,
        config,
        account_summary,
        symbols_override=symbols_override,
        fast_lane=fast_lane,
    )
    best = next((item for item in scan["candidates"] if item.get("passed")), None)
    if not best:
        return {
            "action": "WAIT",
            "reason": "no_candidate_passed",
            "scan": scan,
            "risk": {"allowed": False, "reason": "no_candidate_passed"},
        }
    bars = client.klines_history(best["symbol"], scan["mode"]["interval"], int(scan["mode"]["recent_days"]))
    decision = build_stage1_decision(best["symbol"], bars, config, state, account_summary, scan_candidate=best)
    if (decision.get("risk") or {}).get("reason") == "max_open_positions":
        rotation = build_position_rotation_plan(best, scan, config, state, account_summary)
        if rotation.get("allowed"):
            decision = build_stage1_decision(
                best["symbol"],
                bars,
                config,
                state,
                account_summary,
                scan_candidate=best,
                risk_overrides={"ignore_max_open_positions": True},
            )
        else:
            decision["action"] = "WAIT"
        decision["rotation"] = rotation
    decision["scan"] = scan
    decision["candidate"] = best
    return decision


def close_rotation_position(client: BinanceFuturesClient, position: dict[str, Any]) -> dict[str, Any]:
    symbol = position_symbol(position)
    direction = position_direction(position)
    live_position = find_live_position(client, symbol, direction) if hasattr(client, "account_live") else position
    if live_position is None:
        return {
            "symbol": symbol,
            "direction": direction,
            "quantity": 0.0,
            "skipped": True,
            "reason": "position_already_closed",
        }
    quantity = position_amount_abs(live_position)
    close_side = "BUY" if direction == "SHORT" else "SELL"
    position_side = None
    try:
        if client.position_side_dual().get("dualSidePosition") is True:
            position_side = direction
    except Exception:
        position_side = None
    cancelled_orders = client.cancel_all_open_orders(symbol)
    cancelled_algo_orders = client.cancel_all_open_algo_orders(symbol)
    try:
        close_order = client.place_market_order(
            symbol=symbol,
            side=close_side,
            quantity=quantity,
            position_side=position_side,
        )
    except RuntimeError as exc:
        if not is_reduce_only_rejection(exc):
            raise
        if find_live_position(client, symbol, direction) is not None:
            raise
        return {
            "symbol": symbol,
            "direction": direction,
            "quantity": quantity,
            "cancelled_orders": cancelled_orders,
            "cancelled_algo_orders": cancelled_algo_orders,
            "skipped": True,
            "reason": "position_already_closed_after_cancel",
            "error": str(exc),
        }
    return {
        "symbol": symbol,
        "direction": direction,
        "quantity": quantity,
        "cancelled_orders": cancelled_orders,
        "cancelled_algo_orders": cancelled_algo_orders,
        "close_order": close_order,
    }


def build_grid_decisions(config: dict[str, Any], account_summary: dict[str, Any], klines: dict[str, list[list[Any]]]) -> list[dict[str, Any]]:
    equity = account_summary.get("equity") or 0
    if equity <= 0:
        return [{"status": "WAIT", "reason": "account_unavailable"}]
    return [
        build_grid_plan(symbol, bars, config, equity)
        for symbol, bars in klines.items()
    ]


def execute_stage1_market_order(
    client: BinanceFuturesClient,
    decision: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    if decision.get("action") not in {"OPEN_LONG", "OPEN_SHORT"}:
        return {"mode": "none", "message": "No executable decision."}
    filters = ExchangeFilters(client.exchange_info())
    symbol = decision["symbol"]
    quantity = filters.quantity(symbol, float(decision["quantity"]))
    protection_plan = decision.get("protection_plan") or (decision.get("signal") or {}).get("protection_plan") or {}
    if protection_plan.get("enabled"):
        stop_value = protection_plan.get("initial_stop", decision["signal"]["stop"])
        take_profit_value = protection_plan.get("initial_take_profit", decision["signal"]["take_profit"])
    else:
        stop_value = decision["signal"]["stop"]
        take_profit_value = decision["signal"]["take_profit"]
    stop = filters.price(symbol, float(stop_value))
    take_profit = filters.price(symbol, float(take_profit_value))
    entry_price = float(decision["signal"]["last_price"])
    notional = quantity * entry_price
    min_notional = filters.min_notional(symbol)
    min_notional_with_buffer = min_notional * (1 + max(float(config.get("min_order_notional_buffer_pct", 3.0)), 0.0) / 100)
    effective_min_notional = float(config.get("effective_min_order_notional_usdt", 10.0))
    max_notional = float((decision.get("risk") or {}).get("max_notional") or 0)
    if not config.get("effective_position_sizing_enabled", True) and 0 < notional < min_notional_with_buffer:
        min_quantity = filters.min_quantity_for_notional(
            symbol,
            entry_price,
            buffer_pct=float(config.get("min_order_notional_buffer_pct", 3.0)),
        )
        min_quantity_notional = min_quantity * entry_price
        if min_quantity > quantity and (max_notional <= 0 or min_quantity_notional <= max_notional):
            quantity = min_quantity
            notional = min_quantity_notional
    direction = str(decision.get("direction") or decision.get("signal", {}).get("signal") or "LONG").upper()
    entry_side = "SELL" if direction == "SHORT" else "BUY"
    close_side = "BUY" if direction == "SHORT" else "SELL"
    order = {
        "symbol": symbol,
        "side": entry_side,
        "direction": direction,
        "quantity": quantity,
        "stop": stop,
        "take_profit": take_profit,
        "notional": notional,
        "protection_plan": protection_plan,
    }
    required_notional = max(min_notional, effective_min_notional) if config.get("effective_position_sizing_enabled", True) else min_notional
    if quantity <= 0 or notional < required_notional:
        return {"mode": "blocked", "message": "Quantity is below effective order minimum.", "order": order}
    rotation = decision.get("rotation") or {}
    if not live_trading_allowed(config):
        if rotation.get("allowed"):
            return {"mode": "rotation_dry_run", "order": order, "rotation": rotation}
        return {"mode": "dry_run", "order": order}
    leverage = max(1, min(50, int(float(decision.get("leverage", config.get("stage1_max_leverage", 2))))))
    client.set_leverage(symbol, leverage)
    position_side = None
    try:
        if client.position_side_dual().get("dualSidePosition") is True:
            position_side = direction
            order["position_side"] = position_side
    except Exception:
        position_side = None
    rotation_close = None
    if rotation.get("allowed"):
        rotation_close = close_rotation_position(client, rotation.get("from", {}))
    entry_order = client.place_market_order(symbol=symbol, side=entry_side, quantity=quantity, position_side=position_side)
    try:
        stop_order = client.place_algo_order(
            symbol=symbol,
            side=close_side,
            order_type="STOP_MARKET",
            trigger_price=stop,
            position_side=position_side,
        )
        take_profit_order = client.place_algo_order(
            symbol=symbol,
            side=close_side,
            order_type="TAKE_PROFIT_MARKET",
            trigger_price=take_profit,
            position_side=position_side,
        )
    except Exception as exc:
        close_order = client.place_market_order(
            symbol=symbol,
            side=close_side,
            quantity=quantity,
            position_side=position_side,
        )
        return {
            "mode": "protection_failed_closed",
            "entry_order": entry_order,
            "close_order": close_order,
            "error": str(exc),
        }
    live_position = find_live_position(client, symbol, direction)
    if live_position is not None:
        try:
            live_position = enrich_positions_with_prices([live_position], client.position_risk())[0]
            audit = audit_position_protection(
                client,
                live_position,
                config,
                repair=False,
            )
        except Exception as exc:
            audit = {"protected": False, "status": "audit_error", "error": str(exc)}
        if not audit.get("protected"):
            close_order = client.place_market_order(
                symbol=symbol,
                side=close_side,
                quantity=position_amount_abs(live_position),
                position_side=position_side,
            )
            return {
                "mode": "protection_confirm_failed_closed",
                "entry_order": entry_order,
                "stop_order": stop_order,
                "take_profit_order": take_profit_order,
                "close_order": close_order,
                "protection_audit": audit,
            }
    return {
        "mode": "rotation_live" if rotation_close else "live",
        "rotation_close": rotation_close,
        "entry_order": entry_order,
        "stop_order": stop_order,
        "take_profit_order": take_profit_order,
        "protection_audit": audit if "audit" in locals() else None,
    }


def execute_grid_orders(
    client: BinanceFuturesClient,
    plan: dict[str, Any],
    config: dict[str, Any],
    position_amount: float = 0.0,
) -> dict[str, Any]:
    if plan.get("status") != "READY":
        return {"mode": "none", "message": "Grid plan is not ready.", "plan": plan}

    filters = ExchangeFilters(client.exchange_info())
    symbol = plan["symbol"]
    raw_orders = build_grid_orders(plan, config, position_amount=position_amount)
    orders = []
    for raw in raw_orders:
        price = filters.price(symbol, float(raw["price"]))
        quantity = filters.quantity(symbol, float(raw["quantity"]))
        if quantity * price >= filters.min_notional(symbol):
            orders.append({**raw, "price": price, "quantity": quantity})

    if not live_trading_allowed(config):
        return {"mode": "dry_run", "symbol": symbol, "orders": orders, "count": len(orders)}

    leverage = max(1, min(5, int(float(config.get("stage2_max_leverage", 1.5)))))
    client.set_leverage(symbol, leverage)
    client.cancel_all_open_orders(symbol)
    placed = [
        client.place_limit_order(
            symbol=symbol,
            side=order["side"],
            quantity=order["quantity"],
            price=order["price"],
            reduce_only=bool(order["reduce_only"]),
        )
        for order in orders
    ]
    return {"mode": "live", "symbol": symbol, "placed": placed, "count": len(placed)}
