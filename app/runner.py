from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from app.account_projection import canonical_account_projection
from app.binance_client import BinanceFuturesClient
from app.binance_rate import BinanceRateLimitError, rate_status, request_priority
from app.config_store import load_config
from app.learning_report import save_daily_learning_report
from app.live_learning import sync_live_learning_from_binance
from app.live_reaction import sync_live_reaction_from_binance
from app.local_circuit import record_v4_live_open
from app.market_stream import start_market_stream_thread
from app.opportunity_queue import read_opportunities
from app.opportunity_v4 import V4_CONTROL_FAMILY, V4_STRATEGY_FAMILY
from app.performance_guard import global_performance_guard, update_release_equity_guard
from app.protection_audit import audit_account_protection
from app.recovery_controller import consume_recovery_permit, revoke_recovery_permit
from app.strategy_canary import consume_strategy_canary, revoke_strategy_canary
from app.risk import direction_cooldown_key, live_trading_allowed
from app.runtime_protection import manage_runtime_protection
from app.shadow_trading import update_shadow_trades
from app.stage_modes import apply_stage_route
from app.trading_engine import (
    build_best_growth_decision,
    build_grid_decisions,
    close_rotation_position,
    execute_grid_orders,
    execute_stage1_market_order,
    is_reduce_only_rejection,
    summarize_account,
    sync_stage,
)
from app.state_store import load_state, save_state
from app.runtime_snapshot import market_rows_from_scan, update_runtime_snapshot
from app.telemetry import compact_decision, maintain_telemetry, record_equity_snapshot, record_event, record_event_throttled, record_strategy_run
from app.user_stream import start_user_stream_thread


_EXECUTION_LOCK = threading.Lock()
_PROTECTION_LOCK = threading.Lock()
_PROTECTION_AUDIT_CACHE: dict[str, object] = {
    "checked_monotonic": 0.0,
    "fingerprint": None,
    "result": None,
}
_BACKGROUND_SCAN_STATE_LOCK = threading.Lock()
_BACKGROUND_SCAN_STATE: dict[str, object] = {
    "in_flight": False,
    "started_monotonic": None,
    "started_at": None,
    "completed_at": None,
    "last_error": "",
    "restart_reason": "",
}


def _set_background_scan_state(**updates: object) -> dict[str, object]:
    with _BACKGROUND_SCAN_STATE_LOCK:
        _BACKGROUND_SCAN_STATE.update(updates)
        return dict(_BACKGROUND_SCAN_STATE)


def background_scan_watchdog_reason(
    scan_state: dict[str, object],
    *,
    thread_alive: bool,
    now_monotonic: float,
    timeout_seconds: float,
) -> str | None:
    if not thread_alive:
        return "background scan thread exited"
    started = scan_state.get("started_monotonic")
    if scan_state.get("in_flight") and started is not None:
        elapsed = now_monotonic - float(started)
        if elapsed > timeout_seconds:
            return f"background scan stalled for {elapsed:.1f}s"
    return None


def _runtime_account(account: dict) -> dict:
    return {
        "equity": account.get("equity"),
        "available_balance": account.get("available_balance"),
        "unrealized_pnl": account.get("unrealized_pnl"),
        "positions": list(account.get("positions") or []),
    }


def _audit_account_protection_cached(
    client: BinanceFuturesClient,
    config: dict,
    account: dict,
    *,
    repair: bool,
    minimum_interval_seconds: float,
) -> dict:
    fingerprint = tuple(sorted(_position_keys(account)))
    with _PROTECTION_LOCK:
        now = time.monotonic()
        cached_result = _PROTECTION_AUDIT_CACHE.get("result")
        if (
            cached_result is not None
            and _PROTECTION_AUDIT_CACHE.get("fingerprint") == fingerprint
            and now - float(_PROTECTION_AUDIT_CACHE.get("checked_monotonic") or 0) < minimum_interval_seconds
        ):
            return dict(cached_result)
        with request_priority("critical"):
            result = audit_account_protection(client, config, account, repair=repair)
        _PROTECTION_AUDIT_CACHE.update(
            checked_monotonic=now,
            fingerprint=fingerprint,
            result=dict(result),
        )
        return result


def enforce_hard_stop(
    client: BinanceFuturesClient,
    config: dict,
    account: dict,
) -> dict:
    """Flatten live exposure once at the absolute account floor."""
    equity = float(account.get("equity") or 0)
    floor = float(config.get("hard_stop_equity", config.get("tournament_stop_equity", 5.0)))
    if floor <= 0 or equity > floor:
        return {"triggered": False, "equity": equity, "floor": floor}
    actions = []
    if live_trading_allowed(config):
        for position in account.get("positions", []) or []:
            if abs(float(position.get("positionAmt") or position.get("amount") or 0)) <= 0:
                continue
            actions.append(close_rotation_position(client, position))
        symbols = {
            str(order.get("symbol") or "").upper()
            for order in (client.open_orders() or []) + (client.open_algo_orders() or [])
            if order.get("symbol")
        }
        for symbol in symbols:
            client.cancel_all_open_orders(symbol)
            client.cancel_all_open_algo_orders(symbol)
    save_state(
        {
            "bot_status": "hard_stopped",
            "hard_stop_triggered": True,
            "hard_stop_reason": f"equity {equity:.4f}U <= hard stop {floor:.2f}U",
            "last_error": "账户权益触发 5U 硬停止线，已禁止新仓。",
        }
    )
    record_event(
        "error",
        "hard_stop",
        "账户权益触发硬停止线，系统已退出持仓并停止新交易。",
        {"equity": equity, "floor": floor, "actions": actions},
    )
    return {"triggered": True, "equity": equity, "floor": floor, "actions": actions}


def is_min_notional_rejection(exc: Exception) -> bool:
    text = str(exc)
    return "-4164" in text or "notional must be no smaller than" in text


def has_live_position(client: BinanceFuturesClient) -> bool:
    account = summarize_account(client.account_live())
    return any(abs(float(position.get("positionAmt", 0) or 0)) > 0 for position in account.get("positions", []))


def loop_seconds_for(config: dict, mode: str | None) -> int:
    mode = mode or "balanced"
    return int(config.get(f"{mode}_loop_seconds", os.getenv("BOT_LOOP_SECONDS", "300")))


def background_loop_seconds(config: dict, result: dict | None = None) -> int:
    requested = int((result or {}).get("loop_seconds") or os.getenv("BOT_LOOP_SECONDS", "300"))
    minimum = int(config.get("background_scan_min_interval_seconds", 30))
    return max(10, minimum, requested)


def set_symbol_cooldown(state: dict, symbol: str, minutes: float) -> None:
    if minutes <= 0:
        return
    cooldowns = dict(state.get("symbol_cooldowns") or {})
    cooldowns[symbol.upper()] = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
    save_state({"symbol_cooldowns": cooldowns})


def set_symbol_direction_cooldown(state: dict, symbol: str, direction: str, minutes: float) -> None:
    if minutes <= 0:
        return
    cooldowns = dict(state.get("symbol_direction_cooldowns") or {})
    cooldowns[direction_cooldown_key(symbol, direction)] = (
        datetime.now(timezone.utc) + timedelta(minutes=minutes)
    ).isoformat()
    save_state({"symbol_direction_cooldowns": cooldowns})


def set_rotation_cooldown(state: dict, symbol: str, minutes: float) -> None:
    cooldowns = dict(state.get("rotation_cooldowns") or {})
    cooldowns[symbol.upper()] = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
    save_state({"rotation_cooldowns": cooldowns})


def track_runtime_position(decision: dict, result: dict | None = None) -> None:
    symbol = str(decision.get("symbol") or "").upper()
    direction = str(decision.get("direction") or (decision.get("signal") or {}).get("signal") or "LONG").upper()
    if not symbol or direction not in {"LONG", "SHORT"}:
        return
    protection_plan = decision.get("protection_plan") or (decision.get("signal") or {}).get("protection_plan") or {}
    protection_profile = (decision.get("signal") or {}).get("protection_profile") or {}
    candidate = decision.get("candidate") or {}
    strategy_family = str(candidate.get("strategy_family") or decision.get("strategy_family") or "")
    opportunity_v4 = candidate.get("opportunity_v4") or {}
    performance_guard = candidate.get("global_performance_guard") or {}
    strategy_canary = performance_guard.get("strategy_canary_permit") or {}
    effective_risk = decision.get("effective_risk") or {}
    full_bet_sizing = decision.get("full_bet_sizing") or {}
    entry_order = (result or {}).get("entry_order") or {}
    initial_quantity = float(
        entry_order.get("executedQty")
        or entry_order.get("origQty")
        or decision.get("quantity")
        or 0.0
    )
    tracked = dict(load_state().get("runtime_protection_positions") or {})
    tracked[f"{symbol}:{direction}"] = {
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "entry_type": decision.get("entry_type"),
        "max_hold_bars": protection_plan.get("max_hold_bars"),
        "max_hold_seconds": protection_profile.get("max_hold_seconds"),
        "strategy_family": strategy_family,
        "strategy_version": str(opportunity_v4.get("strategy_version") or candidate.get("strategy_version") or ""),
        "protection_version": "v5_dynamic" if strategy_family in {"extreme_v3_roll", "extreme_v4_roll"} else protection_profile.get("protection_version"),
        "break_even_atr": protection_profile.get("break_even_atr"),
        "trailing_trigger_atr": protection_profile.get("trailing_trigger_atr"),
        "trailing_distance_atr": protection_profile.get("trailing_distance_atr"),
        "initial_quantity": initial_quantity,
        "initial_risk_pct": float(effective_risk.get("final_risk_pct") or decision.get("risk_pct") or 0.0),
        "leverage": float(decision.get("leverage") or 1.0),
        "full_bet_profile": full_bet_sizing.get("profile"),
        "margin_utilization_pct": full_bet_sizing.get("margin_utilization_pct"),
        "stressed_risk_pct": full_bet_sizing.get("stressed_risk_pct"),
        "position_confidence": dict(opportunity_v4.get("position_confidence") or {}),
        "strategy_canary_permit_id": strategy_canary.get("permit_id"),
        "strategy_canary_multiplier": strategy_canary.get("risk_multiplier"),
        "add_on_attempted": False,
        "add_on_executed": False,
    }
    save_state({"runtime_protection_positions": tracked})


def synthetic_account(equity: float = 50.0) -> dict:
    return {"equity": equity, "available_balance": equity, "unrealized_pnl": 0.0, "positions": []}


def private_api_error(exc: Exception) -> str:
    return (
        "Binance 私有接口鉴权失败，请检查 API Key/Secret、U 本位合约权限、IP 白名单和系统时间。"
        f" 原始错误：{exc}"
    )


def maybe_sync_live_learning(client: BinanceFuturesClient, config: dict, state: dict) -> None:
    if config.get("dry_run", True) or not config.get("live_credit_enabled", True):
        return
    last = state.get("last_live_learning_sync")
    min_seconds = int(config.get("live_credit_sync_seconds", 600))
    now = datetime.now(timezone.utc)
    if last:
        try:
            if (now - datetime.fromisoformat(last)).total_seconds() < min_seconds:
                return
        except ValueError:
            pass
    try:
        result = sync_live_learning_from_binance(client, config)
        save_state({"last_live_learning_sync": now.isoformat(), "last_live_learning_records": result.get("records", 0)})
    except Exception as exc:
        record_event("warning", "live_learning", f"实盘信用分同步失败：{exc}")



def maybe_sync_live_reaction(
    client: BinanceFuturesClient,
    config: dict,
    state: dict,
    account: dict,
    symbols: list[str] | None = None,
) -> None:
    if config.get("dry_run", True) or not config.get("live_reaction_enabled", True):
        return
    last = state.get("last_live_reaction_sync")
    min_seconds = int(config.get("live_reaction_check_seconds", 20))
    if not symbols:
        min_seconds = int(config.get("live_reaction_background_check_seconds", max(180, min_seconds)))
    now = datetime.now(timezone.utc)
    if last:
        try:
            if (now - datetime.fromisoformat(last)).total_seconds() < min_seconds:
                return
        except ValueError:
            pass
    try:
        result = sync_live_reaction_from_binance(
            client,
            config,
            account=account,
            symbols=symbols,
            equity=float(account.get("equity") or 0),
        )
        save_state(
            {
                "last_live_reaction_sync": now.isoformat(),
                "last_live_reaction_records": result.get("records", 0),
                "last_live_reaction_symbols": result.get("symbols", []),
            }
        )
    except BinanceRateLimitError as exc:
        if not symbols:
            record_event_throttled(
                "info",
                "live_reaction",
                "后台实时风控同步因 REST 预算预留跳过",
                {"retry_after": exc.retry_after},
                throttle_seconds=300,
            )
            return
        record_event_throttled(
            "warning",
            "live_reaction",
            f"实时风控同步失败：{exc}",
            {"symbols": symbols or []},
            throttle_seconds=60,
        )
    except Exception as exc:
        record_event_throttled(
            "warning",
            "live_reaction",
            f"实时风控同步失败：{exc}",
            {"symbols": symbols or []},
            throttle_seconds=60,
        )


def maybe_generate_daily_report(config: dict, state: dict) -> None:
    if not config.get("daily_learning_report_enabled", True):
        return
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    if state.get("last_daily_learning_report_date") == today:
        return
    try:
        report = save_daily_learning_report(now)
        save_state({"last_daily_learning_report_date": today, "last_daily_learning_report_path": report.get("path")})
        record_event("info", "daily_learning_report", "daily learning report generated", {"path": report.get("path")})
    except Exception as exc:
        record_event("warning", "daily_learning_report", f"daily report failed: {exc}")

def is_timestamp_error(exc: Exception) -> bool:
    message = str(exc)
    return "-1021" in message or "recvWindow" in message or "Timestamp for this request" in message


def _position_keys(account: dict) -> set[tuple[str, str, float]]:
    return {
        (
            str(position.get("symbol") or ""),
            str(position.get("positionSide") or "BOTH"),
            round(float(position.get("positionAmt", 0) or 0), 12),
        )
        for position in account.get("positions", [])
        if abs(float(position.get("positionAmt", 0) or 0)) > 0
    }


def execute_with_freshness_guard(client: BinanceFuturesClient, decision: dict, config: dict, account: dict) -> dict:
    if decision.get("action") not in {"OPEN_LONG", "OPEN_SHORT"}:
        return execute_stage1_market_order(client, decision, config)
    with _EXECUTION_LOCK, request_priority("critical"):
        fresh_account = summarize_account(client.account_live())
        if _position_keys(fresh_account) != _position_keys(account):
            return {
                "mode": "blocked",
                "message": "持仓在决策期间发生变化，本次信号作废并等待重新评估。",
                "reason": "stale_position_snapshot",
            }
        sizing = decision.get("full_bet_sizing") or {}
        sized_balance = sizing.get("sizing_available_balance")
        fresh_balance = fresh_account.get("available_balance")
        if sized_balance is not None and fresh_balance is not None:
            sized_balance = max(0.0, float(sized_balance))
            fresh_balance = max(0.0, float(fresh_balance))
            mismatch_pct = abs(fresh_balance - sized_balance) / max(sized_balance, 1e-9) * 100.0
            allowed_mismatch = float(config.get("account_projection_balance_mismatch_pct", 2.0))
            if mismatch_pct > allowed_mismatch:
                return {
                    "mode": "blocked",
                    "message": "下单前可用余额与决策快照不一致，已阻止使用过期仓位下单",
                    "reason": "stale_available_balance",
                    "sized_available_balance": sized_balance,
                    "fresh_available_balance": fresh_balance,
                    "mismatch_pct": round(mismatch_pct, 4),
                }
        return execute_stage1_market_order(client, decision, config)


def stage4_scalp_overlay_config(config: dict, state: dict) -> dict | None:
    route = state.get("stage_route") or {}
    if route.get("stage") != "S4" or not config.get("stage_s4_scalp_overlay_enabled", True):
        return None
    overlay_route = {
        **route,
        "label": "网格叠加盘口剥头皮",
        "mode": "yolo_scalp",
        "recommended_mode": "yolo_scalp",
        "strategy_family": "orderbook_scalp",
        "risk_pct": float(config.get("stage_s4_scalp_risk_pct", 0.1)),
        "base_risk_pct": float(config.get("stage_s4_scalp_risk_pct", 0.1)),
        "margin_pct": float(config.get("stage_s4_scalp_margin_pct", 5.0)),
        "daily_loss_limit_pct": float(config.get("stage_s4_scalp_daily_loss_limit_pct", 1.0)),
    }
    return apply_stage_route(
        {**config, "_excluded_scan_symbols": list(config.get("stage2_symbols", ["BTCUSDT", "ETHUSDT"]))},
        overlay_route,
    )


def run_once(symbols_override: list[str] | None = None, fast_lane: bool = False) -> dict:
    cycle_started = time.perf_counter()
    config = load_config()
    state = load_state()
    client = BinanceFuturesClient(
        api_key=config.get("api_key", ""),
        api_secret=config.get("api_secret", ""),
        base_url=config.get("binance_base_url", "https://fapi.binance.com"),
    )
    if state.get("bot_status") != "running":
        record_event("info", "runner", "机器人暂停，跳过本轮扫描")
        return {"status": "paused", "loop_seconds": loop_seconds_for(config, config.get("growth_mode"))}

    if config.get("dry_run", True):
        account = synthetic_account()
        if config.get("api_key") and config.get("api_secret"):
            record_event("warning", "binance_auth", "模拟交易模式使用 50U 模拟账户，不依赖 Binance 私有接口。")
    elif config.get("api_key") and config.get("api_secret"):
        try:
            projection = canonical_account_projection(
                client,
                websocket_max_age_seconds=int(config.get("account_projection_ws_max_age_seconds", 45)),
                require_fresh_available_balance=bool(config.get("account_projection_refresh_before_sizing", True)),
                available_balance_max_age_seconds=int(config.get("account_projection_available_balance_max_age_seconds", 15)),
            )
            account = projection["account"]
            account["projection"] = {
                "source": projection.get("source"),
                "as_of": projection.get("as_of"),
                "fresh_available_balance": projection.get("fresh_available_balance"),
                "available_balance_age_seconds": projection.get("available_balance_age_seconds"),
            }
        except BinanceRateLimitError:
            raise
        except Exception as exc:
            raise RuntimeError(private_api_error(exc)) from exc
    else:
        raise RuntimeError("实盘模式需要先配置 Binance API Key 和 Secret。")
    warning_floor = float(config.get("risk_warning_equity", 30.0))
    warning_active = warning_floor > 0 and float(account.get("equity") or 0) < warning_floor
    if bool(state.get("risk_warning_active")) != warning_active:
        save_state({"risk_warning_active": warning_active})
    hard_stop = enforce_hard_stop(client, config, account)
    if hard_stop.get("triggered"):
        return {"status": "hard_stopped", "hard_stop": hard_stop, "loop_seconds": loop_seconds_for(config, config.get("growth_mode"))}
    state = sync_stage(config, state, account)
    config = apply_stage_route(config, state.get("stage_route"))
    release_equity_guard = update_release_equity_guard(config, account.get("equity"))
    config["_strategy_release_equity_guard"] = release_equity_guard
    config["_release_fallback_active"] = bool(release_equity_guard.get("fallback_active"))
    state = {**state, "strategy_release_equity_guard": release_equity_guard}
    performance_status = global_performance_guard(config, account.get("equity"))
    state = {
        **state,
        "strategy_canary": performance_status.get("strategy_canary_permit")
        or state.get("strategy_canary")
        or {},
    }
    maybe_sync_live_reaction(client, config, state, account, symbols_override)
    if not fast_lane:
        maybe_sync_live_learning(client, config, state)
        audit_status = _audit_account_protection_cached(
            client,
            config,
            account,
            repair=live_trading_allowed(config),
            minimum_interval_seconds=float(config.get("account_supervisor_position_audit_seconds", 10)),
        )
        if audit_status.get("positions") and not audit_status.get("protected", True):
            record_event_throttled(
                "warning",
                "protection_audit",
                "unprotected position detected",
                audit_status,
                throttle_seconds=int(config.get("protection_audit_log_throttle_seconds", 60)),
            )
        protection_status = manage_runtime_protection(client, config, state, account)
        if protection_status.get("actions"):
            noisy_actions = [
                action for action in protection_status.get("actions", [])
                if action.get("action") != "observe" or action.get("reason") not in {"holding"}
            ]
            if noisy_actions:
                record_event("info", "runtime_protection", "runtime protection checked", {**protection_status, "actions": noisy_actions})
        maybe_generate_daily_report(config, state)

    grid_results: list[dict] = []
    if state.get("stage") == "grid":
        if not fast_lane:
            for symbol in config.get("stage2_symbols", ["BTCUSDT", "ETHUSDT"]):
                bars = client.klines(symbol.upper(), config["interval"], int(config["limit"]))
                plan = build_grid_decisions(config, account, {symbol.upper(): bars})[0]
                position_amount = 0.0
                for position in account.get("positions", []):
                    if position.get("symbol") == symbol.upper():
                        position_amount = float(position.get("positionAmt", 0))
                result = execute_grid_orders(client, plan, config, position_amount=position_amount)
                grid_results.append(result)
                record_strategy_run(
                    state,
                    account,
                    {"symbol": symbol.upper(), "action": plan.get("status"), "reason": plan.get("reason"), "signal": {}, "scan": {}, "candidate": plan},
                    result,
                )
            record_equity_snapshot(account, state, mode="grid", action="grid_checked", reason="grid_loop")
            record_event("info", "grid", "完成网格检查", {"results": grid_results})
        overlay_config = stage4_scalp_overlay_config(config, state)
        if overlay_config is None:
            return {"status": "grid_checked", "results": grid_results, "loop_seconds": int(config.get("grid_loop_seconds", 300))}
        config = overlay_config
        excluded = {str(symbol).upper() for symbol in config.get("_excluded_scan_symbols", [])}
        if symbols_override is not None:
            symbols_override = [symbol for symbol in symbols_override if symbol.upper() not in excluded]
            if fast_lane and not symbols_override:
                return {"status": "grid_event_ignored", "results": grid_results, "loop_seconds": int(config.get("grid_loop_seconds", 300))}

    decision = build_best_growth_decision(
        client,
        config,
        state,
        account,
        symbols_override=symbols_override,
        fast_lane=fast_lane,
    )
    scan = decision.get("scan") or {}
    shadow_candidates = [item for item in scan.get("candidates", []) if not item.get("passed")]
    paired_active_candidates: list[dict[str, Any]] = []
    if config.get("opportunity_v33_challenger_enabled", False) and not config.get("opportunity_v4_enabled", True):
        for item in scan.get("candidates", []):
            challenger = item.get("v33_challenger") or {}
            if not challenger.get("eligible"):
                continue
            paired_active_candidates.append(
                {
                    **item,
                    "passed": False,
                    "decision_reason": "V3.3 配对实验的 V3.2 同场基线",
                }
            )
            signal = dict(item.get("signal") or {})
            profile = dict(challenger.get("protection_profile") or signal.get("protection_profile") or {})
            atr_value = float(signal.get("atr") or 0)
            entry = float(signal.get("last_price") or 0)
            direction = str(item.get("direction") or signal.get("signal") or "LONG").upper()
            take_atr = float(profile.get("take_profit_atr") or 0)
            if entry > 0 and atr_value > 0 and take_atr > 0:
                signal["take_profit"] = entry - atr_value * take_atr if direction == "SHORT" else entry + atr_value * take_atr
            signal["protection_profile"] = profile
            shadow_candidates.append(
                {
                    **item,
                    "strategy": "opportunity_v33_candidate",
                    "strategy_family": "extreme_v3_roll",
                    "strategy_version": challenger.get("strategy_version") or config.get("opportunity_v33_strategy_version"),
                    "strategy_role": "challenger",
                    "strategy_generation": "v3.3-shadow",
                    "score": challenger.get("score"),
                    "passed": False,
                    "decision_reason": challenger.get("reason"),
                    "signal": signal,
                    "opportunity_v33": challenger,
                }
            )
    shadow_candidates.extend(paired_active_candidates)
    if config.get("opportunity_v4_enabled", True):
        decision_limit = int(config.get("opportunity_v4_decision_shadow_limit", 3))
        exploration_limit = int(config.get("opportunity_v4_exploration_shadow_limit", 6))
        control_limit = int(config.get("opportunity_v4_control_shadow_limit", 3))
        decision_count = 0
        exploration_count = 0
        control_count = 0
        v4_version = str(config.get("opportunity_v4_strategy_version") or "v4.3.2")
        v4_shadow_role = "active" if config.get("opportunity_v4_live_enabled", False) else "challenger"
        v4_rows = list(scan.get("v4_candidates") or scan.get("candidates", []))
        decision_rows = [item for item in v4_rows if (item.get("opportunity_v4") or {}).get("decision_candidate")]
        exploration_pool = [item for item in v4_rows if not (item.get("opportunity_v4") or {}).get("decision_candidate")]
        exploration_rows: list[dict] = []
        seen_groups: set[tuple[str, str]] = set()
        for item in exploration_pool:
            v4 = item.get("opportunity_v4") or {}
            group = (str(v4.get("rank_bucket") or "unknown"), str(item.get("entry_type") or "unknown"))
            if group in seen_groups:
                continue
            seen_groups.add(group)
            exploration_rows.append(item)
        for item in exploration_pool:
            if item not in exploration_rows:
                exploration_rows.append(item)
        for item in decision_rows + exploration_rows:
            v4 = item.get("opportunity_v4") or {}
            if not v4.get("shadow_eligible"):
                continue
            is_decision = bool(v4.get("decision_candidate")) and decision_count < decision_limit
            if not is_decision and exploration_count >= exploration_limit:
                continue
            evidence_type = "decision" if is_decision else "exploration"
            if is_decision:
                decision_count += 1
            else:
                exploration_count += 1
            shadow_candidates.append(
                {
                    **item,
                    "strategy": "opportunity_v4_candidate",
                    "strategy_family": V4_STRATEGY_FAMILY,
                    "strategy_version": v4_version,
                    "strategy_role": v4_shadow_role,
                    "strategy_generation": "v4-shadow",
                    "evidence_type": evidence_type,
                    "score": float(v4.get("score") or item.get("score") or 0),
                    "passed": False,
                    "decision_reason": v4.get("reason"),
                    "opportunity_v4": v4,
                }
            )
            if not is_decision or control_count >= control_limit or str(item.get("entry_type")) != "v3_breakout":
                continue
            signal = dict(item.get("signal") or {})
            entry = float(signal.get("last_price") or 0)
            atr_value = float(signal.get("atr") or 0)
            direction = str(item.get("direction") or signal.get("signal") or "LONG").upper()
            if entry <= 0 or atr_value <= 0:
                continue
            signal["stop"] = entry + atr_value if direction == "SHORT" else entry - atr_value
            signal["take_profit"] = entry - atr_value * 2 if direction == "SHORT" else entry + atr_value * 2
            signal["protection_profile"] = {
                "stop_atr": 1.0,
                "take_profit_atr": 2.0,
                "max_hold_bars": 18,
            }
            shadow_candidates.append(
                {
                    **item,
                    "strategy": "v4_simple_breakout_control",
                    "strategy_family": V4_CONTROL_FAMILY,
                    "strategy_version": "simple-breakout-v1",
                    "strategy_role": "challenger",
                    "strategy_generation": "v4-control-shadow",
                    "evidence_type": "paired_control",
                    "shadow_force_eligible": True,
                    "score": float(v4.get("score") or item.get("score") or 0),
                    "passed": False,
                    "decision_reason": "V4 同机会简单趋势突破对照组",
                    "signal": signal,
                    "opportunity_v4": v4,
                }
            )
            control_count += 1
    if decision.get("action") == "WAIT" and (decision.get("candidate") or {}).get("passed"):
        shadow_candidates.append(
            {
                **decision["candidate"],
                "passed": False,
                "decision_reason": decision.get("decision_reason") or (decision.get("risk") or {}).get("reason"),
            }
        )
    shadow_status = update_shadow_trades(shadow_candidates, config)
    try:
        result = execute_with_freshness_guard(client, decision, config, account)
    except RuntimeError as exc:
        if is_reduce_only_rejection(exc) and not has_live_position(client):
            result = {
                "mode": "blocked",
                "message": "Reduce-only close was rejected because no live position remains.",
                "error": str(exc),
            }
            record_event(
                "warning",
                "order_reduce_only_recovered",
                "Binance refused a duplicate close order; no live position remains, so the bot keeps running.",
                {"decision": {"symbol": decision.get("symbol"), "action": decision.get("action")}, "error": str(exc)},
            )
            return {
                "status": "growth_checked",
                "decision": decision,
                "results": grid_results + [result],
                "loop_seconds": int(config.get("grid_loop_seconds", 300)) if state.get("stage") == "grid" else loop_seconds_for(config, ((decision.get("scan") or {}).get("mode") or {}).get("mode")),
            }
        if not is_min_notional_rejection(exc):
            raise
        result = {
            "mode": "blocked",
            "message": "Exchange rejected order below minimum notional.",
            "error": str(exc),
        }
        record_event(
            "warning",
            "order_min_notional",
            "Binance 拒绝了低于最小名义金额的订单，本轮信号已跳过，机器人继续运行。",
            {"decision": {"symbol": decision.get("symbol"), "action": decision.get("action")}, "error": str(exc)},
        )
    if result.get("mode") in {"protection_failed_closed", "protection_confirm_failed_closed"}:
        revoke_recovery_permit("exchange_protection_confirmation_failed")
        revoke_strategy_canary("exchange_protection_confirmation_failed")
    if result.get("mode") in {"live", "rotation_live"} and decision.get("symbol"):
        record_v4_live_open(decision, result)
        consume_recovery_permit(decision, result)
        consume_strategy_canary(decision, result)
        track_runtime_position(decision, result)
        cooldown_minutes = float(config.get("symbol_cooldown_minutes", 0))
        if config.get("directional_cooldown_enabled", True):
            set_symbol_direction_cooldown(
                state,
                decision["symbol"],
                str(decision.get("direction") or (decision.get("signal") or {}).get("signal") or "LONG"),
                cooldown_minutes,
            )
        elif config.get("legacy_symbol_cooldown_blocks", False):
            set_symbol_cooldown(state, decision["symbol"], cooldown_minutes)
    if result.get("mode") == "rotation_live":
        rotation_from = ((decision.get("rotation") or {}).get("from") or {}).get("symbol")
        rotation_minutes = float(config.get("rotation_cooldown_minutes", 45))
        if rotation_from:
            set_rotation_cooldown(state, rotation_from, rotation_minutes)
        if decision.get("symbol"):
            set_rotation_cooldown(state, decision["symbol"], rotation_minutes)
    record_strategy_run(
        state,
        account,
        decision,
        result,
        throttle_seconds=30 if fast_lane and decision.get("action") == "WAIT" else 0,
    )
    best = decision.get("candidate") or scan.get("best") or {}
    record_equity_snapshot(
        account,
        state,
        mode=(scan.get("mode") or {}).get("mode"),
        best=best,
        action=decision.get("action"),
        reason=decision.get("reason") or (decision.get("risk") or {}).get("reason"),
    )
    record_event(
        "info",
        "growth",
        "完成增长模式扫描",
        {
            "channel": "fast_lane" if fast_lane else "background_scan",
            "symbol": decision.get("symbol"),
            "action": decision.get("action"),
            "reason": decision.get("reason") or (decision.get("risk") or {}).get("reason"),
            "result_mode": result.get("mode"),
        },
    )
    elapsed = round(time.perf_counter() - cycle_started, 3)
    channel = "fast_lane" if fast_lane else "background_scan"
    snapshot_updates = {
        "channel": channel,
        "last_cycle": {
            "channel": channel,
            "elapsed_seconds": elapsed,
            "symbol": decision.get("symbol"),
            "action": decision.get("action"),
            "reason": decision.get("reason") or (decision.get("risk") or {}).get("reason"),
        },
        "account": _runtime_account(account),
        "risk_status": {
            "warning_active": warning_active,
            "warning_equity": warning_floor,
            "hard_stop_equity": float(config.get("hard_stop_equity", 5.0)),
            "current_equity": float(account.get("equity") or 0),
            "performance_guard": performance_status,
        },
        "shadow_trading": shadow_status,
    }
    if not fast_lane and "audit_status" in locals():
        snapshot_updates["protection_audit"] = audit_status
    if fast_lane:
        snapshot_updates["fast_lane_decision"] = compact_decision(decision)
    else:
        snapshot_updates["decision"] = compact_decision(decision)
        snapshot_updates["market"] = market_rows_from_scan(scan)
    snapshot_updates["fast_lane" if fast_lane else "background_scan"] = {
        "elapsed_seconds": elapsed,
        "symbols": symbols_override or [],
        "action": decision.get("action"),
        "reason": decision.get("reason") or (decision.get("risk") or {}).get("reason"),
        "funnel": scan.get("funnel") or {},
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    update_runtime_snapshot(**snapshot_updates)
    return {
        "status": "growth_checked",
        "decision": decision,
        "results": grid_results + [result],
        "loop_seconds": int(config.get("grid_loop_seconds", 300)) if grid_results else loop_seconds_for(config, (scan.get("mode") or {}).get("mode")),
    }


def main() -> None:
    load_dotenv()
    start_market_stream_thread(load_config)
    interval_seconds = int(os.getenv("BOT_LOOP_SECONDS", "300"))
    while True:
        try:
            state = load_state()
            rate = rate_status()
            if rate.get("cooldown_active"):
                wait = int(rate.get("cooldown_remaining_seconds") or interval_seconds)
                save_state({"bot_status": "rate_limited", "last_error": rate.get("last_error", "Binance REST 限流等待中")})
                record_event("warning", "binance_rate_limit", "Binance REST 限流等待中", rate)
                print({"status": "rate_limited", "wait_seconds": wait, "rate": rate}, flush=True)
                time.sleep(max(5, min(wait, 300)))
                continue
            if state.get("bot_status") == "rate_limited":
                save_state({"bot_status": "running", "last_error": ""})
            result = run_once()
            interval_seconds = int(result.get("loop_seconds") or interval_seconds)
            print(result, flush=True)
        except Exception as exc:
            if is_timestamp_error(exc):
                save_state({"last_error": str(exc)})
                record_event("warning", "runner_time_sync", str(exc))
                print({"status": "time_sync_retry", "error": str(exc)}, flush=True)
            elif isinstance(exc, BinanceRateLimitError):
                wait = int(exc.retry_after or 600)
                save_state({"last_error": str(exc), "bot_status": "rate_limited"})
                record_event("warning", "binance_rate_limit", str(exc), {"retry_after": wait, "status_code": exc.status_code})
                print({"status": "rate_limited", "error": str(exc), "wait_seconds": wait}, flush=True)
                time.sleep(max(5, min(wait, 300)))
            else:
                save_state({"last_error": str(exc), "bot_status": "paused"})
                record_event("error", "runner", str(exc))
                print({"status": "error", "error": str(exc)}, flush=True)
        time.sleep(interval_seconds)


def _account_supervisor_loop() -> None:
    last_audit_monotonic = 0.0
    last_position_fingerprint = ""
    last_account_revision = 0
    while True:
        started = time.monotonic()
        try:
            config = load_config()
            state = load_state()
            if not config.get("account_supervisor_enabled", True) or config.get("dry_run", True):
                update_runtime_snapshot(
                    account_supervisor={"enabled": False, "reason": "disabled_or_simulation"}
                )
                time.sleep(max(2, int(config.get("account_supervisor_poll_seconds", 2))))
                continue
            client = BinanceFuturesClient(
                api_key=config.get("api_key", ""),
                api_secret=config.get("api_secret", ""),
                base_url=config.get("binance_base_url", "https://fapi.binance.com"),
            )
            projection = canonical_account_projection(
                client,
                websocket_max_age_seconds=int(config.get("account_projection_ws_max_age_seconds", 45)),
            )
            revision = int(projection.get("revision") or 0)
            if revision and revision != last_account_revision:
                projection = canonical_account_projection(
                    client,
                    websocket_max_age_seconds=int(config.get("account_projection_ws_max_age_seconds", 45)),
                    force_rest=True,
                )
                last_account_revision = revision
            account = projection["account"]
            hard_floor = float(config.get("hard_stop_equity", 5.0))
            if (
                hard_floor > 0
                and float(account.get("equity") or 0) <= hard_floor
                and state.get("bot_status") != "hard_stopped"
            ):
                with _EXECUTION_LOCK:
                    enforce_hard_stop(client, config, account)
            positions = list(account.get("positions") or [])
            fingerprint = "|".join(
                sorted(
                    f"{item.get('symbol')}:{item.get('positionSide', 'BOTH')}:{item.get('positionAmt', 0)}:{item.get('entryPrice', 0)}"
                    for item in positions
                )
            )
            now_monotonic = time.monotonic()
            audit_interval = int(
                config.get(
                    "account_supervisor_position_audit_seconds" if positions else "account_supervisor_idle_audit_seconds",
                    10 if positions else 30,
                )
            )
            audit_due = fingerprint != last_position_fingerprint or now_monotonic - last_audit_monotonic >= audit_interval
            runtime_updates = {
                "account": _runtime_account(account),
                "account_projection": {
                    key: projection.get(key)
                    for key in [
                        "source",
                        "as_of",
                        "age_seconds",
                        "stream_age_seconds",
                        "stale",
                        "position_count",
                        "revision",
                    ]
                },
            }
            if audit_due:
                audit = _audit_account_protection_cached(
                    client,
                    config,
                    account,
                    repair=live_trading_allowed(config),
                    minimum_interval_seconds=audit_interval,
                )
                runtime_updates["protection_audit"] = audit
                last_audit_monotonic = now_monotonic
                last_position_fingerprint = fingerprint
                if audit.get("positions") and not audit.get("protected", True):
                    record_event_throttled(
                        "error",
                        "account_supervisor",
                        "持仓保护巡检未通过",
                        audit,
                        throttle_seconds=int(config.get("protection_audit_log_throttle_seconds", 60)),
                    )
            runtime_updates["account_supervisor"] = {
                "enabled": True,
                "healthy": not projection.get("stale"),
                "last_check_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "next_audit_seconds": audit_interval,
                "last_error": "",
            }
            update_runtime_snapshot(**runtime_updates)
        except BinanceRateLimitError as exc:
            update_runtime_snapshot(
                account_supervisor={
                    "enabled": True,
                    "healthy": False,
                    "last_check_at": datetime.now(timezone.utc).isoformat(),
                    "last_error": str(exc),
                    "rate_limited": True,
                }
            )
        except Exception as exc:
            record_event_throttled(
                "error",
                "account_supervisor",
                "账户实时投影或保护巡检异常",
                {"error": str(exc)},
                throttle_seconds=60,
            )
            update_runtime_snapshot(
                account_supervisor={
                    "enabled": True,
                    "healthy": False,
                    "last_check_at": datetime.now(timezone.utc).isoformat(),
                    "last_error": str(exc),
                }
            )
        elapsed = time.monotonic() - started
        poll_seconds = max(1, int(load_config().get("account_supervisor_poll_seconds", 2)))
        time.sleep(max(0.2, poll_seconds - elapsed))


def _background_scan_loop() -> None:
    interval_seconds = int(os.getenv("BOT_LOOP_SECONDS", "300"))
    while True:
        started = time.monotonic()
        try:
            config = load_config()
            state = load_state()
            if state.get("bot_status") != "running":
                _set_background_scan_state(in_flight=False, started_monotonic=None)
                time.sleep(2)
                continue
            _set_background_scan_state(
                in_flight=True,
                started_monotonic=started,
                started_at=datetime.now(timezone.utc).isoformat(),
                last_error="",
            )
            with request_priority("background"):
                result = run_once()
            save_state({"last_error": ""})
            interval_seconds = background_loop_seconds(config, result)
            completed_at = datetime.now(timezone.utc).isoformat()
            scan_state = _set_background_scan_state(
                in_flight=False,
                started_monotonic=None,
                completed_at=completed_at,
                last_error="",
            )
            update_runtime_snapshot(scan_supervisor={**scan_state, "healthy": True})
            print({"status": "background_scan", "elapsed": time.monotonic() - started}, flush=True)
        except BinanceRateLimitError as exc:
            _set_background_scan_state(in_flight=False, started_monotonic=None, last_error=str(exc))
            record_event("warning", "background_scan_deferred", str(exc), {"retry_after": exc.retry_after})
        except Exception as exc:
            _set_background_scan_state(in_flight=False, started_monotonic=None, last_error=str(exc))
            save_state({"last_error": str(exc)})
            record_event("error", "background_scan", str(exc))
        elapsed = time.monotonic() - started
        time.sleep(max(5, interval_seconds - elapsed))


def _background_scan_watchdog_loop(scan_thread: threading.Thread) -> None:
    while True:
        config = load_config()
        state = load_state()
        timeout_seconds = float(config.get("background_scan_timeout_seconds", 90))
        scan_state = _set_background_scan_state()
        reason = None
        if state.get("bot_status") == "running":
            reason = background_scan_watchdog_reason(
                scan_state,
                thread_alive=scan_thread.is_alive(),
                now_monotonic=time.monotonic(),
                timeout_seconds=timeout_seconds,
            )
        supervisor = {
            **scan_state,
            "healthy": reason is None,
            "thread_alive": scan_thread.is_alive(),
            "timeout_seconds": timeout_seconds,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "restart_reason": reason or "",
        }
        update_runtime_snapshot(scan_supervisor=supervisor)
        if reason:
            record_event("error", "background_scan_watchdog", "后台扫描失去响应，准备自动恢复", supervisor)
            if config.get("background_scan_restart_enabled", True):
                time.sleep(1)
                os._exit(75)
        time.sleep(max(1, int(config.get("background_scan_watchdog_seconds", 5))))


def _event_signature(events: list[dict]) -> str:
    return "|".join(
        f"{event.get('symbol')}:{event.get('direction_hint')}:{event.get('updated_at')}"
        for event in events
    )


def coordinator_main() -> None:
    load_dotenv()
    start_market_stream_thread(load_config)
    start_user_stream_thread(load_config)
    threading.Thread(target=_account_supervisor_loop, name="account-supervisor", daemon=True).start()
    scan_thread = threading.Thread(target=_background_scan_loop, name="background-scan", daemon=True)
    scan_thread.start()
    threading.Thread(
        target=_background_scan_watchdog_loop,
        args=(scan_thread,),
        name="background-scan-watchdog",
        daemon=True,
    ).start()
    last_signature = ""
    last_processed_symbols: dict[str, float] = {}
    last_maintenance_day = ""
    while True:
        config = load_config()
        state = load_state()
        today = datetime.now(timezone.utc).date().isoformat()
        if today != last_maintenance_day:
            try:
                maintain_telemetry(
                    int(config.get("telemetry_retention_days", 30)),
                    strategy_run_retention_days=int(config.get("strategy_run_retention_days", 7)),
                    shadow_trade_retention_days=int(config.get("shadow_trade_retention_days", 14)),
                    batch_size=int(config.get("telemetry_maintenance_batch_size", 50_000)),
                    max_batches=int(config.get("telemetry_maintenance_max_batches", 4)),
                )
                last_maintenance_day = today
            except Exception as exc:
                record_event("warning", "telemetry_maintenance", str(exc))
        if state.get("bot_status") == "running" and config.get("fast_lane_enabled", True):
            events = read_opportunities(
                max_age_seconds=int(config.get("fast_lane_event_max_age_seconds", 45)),
                limit=int(config.get("fast_lane_max_symbols", 3)),
            )
            symbol_cooldown = int(config.get("fast_lane_symbol_cooldown_seconds", 10))
            now_monotonic = time.monotonic()
            events = [
                event for event in events
                if now_monotonic - last_processed_symbols.get(str(event.get("symbol") or "").upper(), 0) >= symbol_cooldown
            ]
            signature = _event_signature(events)
            if events and signature and signature != last_signature:
                last_signature = signature
                symbols = list(dict.fromkeys(str(event.get("symbol") or "").upper() for event in events))
                for symbol in symbols:
                    last_processed_symbols[symbol] = now_monotonic
                try:
                    with request_priority("realtime"):
                        result = run_once(symbols_override=symbols, fast_lane=True)
                    save_state({"last_error": ""})
                    print({"status": "fast_lane", "symbols": symbols, "result": result.get("status")}, flush=True)
                except BinanceRateLimitError as exc:
                    record_event("warning", "fast_lane_rate_limit", str(exc), {"symbols": symbols})
                except Exception as exc:
                    record_event("error", "fast_lane", str(exc), {"symbols": symbols})
        time.sleep(max(1, int(config.get("fast_lane_poll_seconds", 2))))


if __name__ == "__main__":
    coordinator_main()
