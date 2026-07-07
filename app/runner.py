from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from app.binance_client import BinanceFuturesClient
from app.binance_rate import BinanceRateLimitError, rate_status, request_priority
from app.config_store import load_config
from app.learning_report import save_daily_learning_report
from app.live_learning import sync_live_learning_from_binance
from app.market_stream import start_market_stream_thread
from app.opportunity_queue import read_opportunities
from app.risk import direction_cooldown_key
from app.runtime_protection import manage_runtime_protection
from app.trading_engine import (
    build_best_growth_decision,
    build_grid_decisions,
    execute_grid_orders,
    execute_stage1_market_order,
    is_reduce_only_rejection,
    summarize_account,
    sync_stage,
)
from app.state_store import load_state, save_state
from app.runtime_snapshot import market_rows_from_scan, update_runtime_snapshot
from app.telemetry import compact_decision, maintain_telemetry, record_equity_snapshot, record_event, record_strategy_run


_EXECUTION_LOCK = threading.Lock()


def is_min_notional_rejection(exc: Exception) -> bool:
    text = str(exc)
    return "-4164" in text or "notional must be no smaller than" in text


def has_live_position(client: BinanceFuturesClient) -> bool:
    account = summarize_account(client.account_live())
    return any(abs(float(position.get("positionAmt", 0) or 0)) > 0 for position in account.get("positions", []))


def loop_seconds_for(config: dict, mode: str | None) -> int:
    mode = mode or "balanced"
    return int(config.get(f"{mode}_loop_seconds", os.getenv("BOT_LOOP_SECONDS", "300")))


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
        return execute_stage1_market_order(client, decision, config)


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
            account = summarize_account(client.account())
        except BinanceRateLimitError:
            raise
        except Exception as exc:
            raise RuntimeError(private_api_error(exc)) from exc
    else:
        raise RuntimeError("实盘模式需要先配置 Binance API Key 和 Secret。")
    state = sync_stage(config, state, account)
    if not fast_lane:
        maybe_sync_live_learning(client, config, state)
        protection_status = manage_runtime_protection(client, config, state, account)
        if protection_status.get("actions"):
            record_event("info", "runtime_protection", "runtime protection checked", protection_status)
        maybe_generate_daily_report(config, state)

    if state.get("stage") == "grid":
        results = []
        for symbol in config.get("stage2_symbols", ["BTCUSDT", "ETHUSDT"]):
            bars = client.klines(symbol.upper(), config["interval"], int(config["limit"]))
            plan = build_grid_decisions(config, account, {symbol.upper(): bars})[0]
            position_amount = 0.0
            for position in account.get("positions", []):
                if position.get("symbol") == symbol.upper():
                    position_amount = float(position.get("positionAmt", 0))
            result = execute_grid_orders(client, plan, config, position_amount=position_amount)
            results.append(result)
            record_strategy_run(
                state,
                account,
                {"symbol": symbol.upper(), "action": plan.get("status"), "reason": plan.get("reason"), "signal": {}, "scan": {}, "candidate": plan},
                result,
            )
        record_equity_snapshot(account, state, mode="grid", action="grid_checked", reason="grid_loop")
        record_event("info", "grid", "完成网格检查", {"results": results})
        return {"status": "grid_checked", "results": results, "loop_seconds": int(config.get("grid_loop_seconds", 300))}

    decision = build_best_growth_decision(
        client,
        config,
        state,
        account,
        symbols_override=symbols_override,
        fast_lane=fast_lane,
    )
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
                "results": [result],
                "loop_seconds": loop_seconds_for(config, ((decision.get("scan") or {}).get("mode") or {}).get("mode")),
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
    if result.get("mode") in {"live", "rotation_live"} and decision.get("symbol"):
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
    record_strategy_run(state, account, decision, result)
    scan = decision.get("scan") or {}
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
        "account": {key: account.get(key) for key in ["equity", "available_balance", "unrealized_pnl"]},
    }
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
        "results": [result],
        "loop_seconds": loop_seconds_for(config, (scan.get("mode") or {}).get("mode")),
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


def _background_scan_loop() -> None:
    interval_seconds = int(os.getenv("BOT_LOOP_SECONDS", "300"))
    while True:
        started = time.monotonic()
        try:
            state = load_state()
            if state.get("bot_status") != "running":
                time.sleep(2)
                continue
            with request_priority("background"):
                result = run_once()
            save_state({"last_error": ""})
            interval_seconds = int(result.get("loop_seconds") or interval_seconds)
            print({"status": "background_scan", "elapsed": time.monotonic() - started}, flush=True)
        except BinanceRateLimitError as exc:
            record_event("warning", "background_scan_deferred", str(exc), {"retry_after": exc.retry_after})
        except Exception as exc:
            save_state({"last_error": str(exc)})
            record_event("error", "background_scan", str(exc))
        elapsed = time.monotonic() - started
        time.sleep(max(5, interval_seconds - elapsed))


def _event_signature(events: list[dict]) -> str:
    return "|".join(
        f"{event.get('symbol')}:{event.get('direction_hint')}:{event.get('updated_at')}"
        for event in events
    )


def coordinator_main() -> None:
    load_dotenv()
    start_market_stream_thread(load_config)
    threading.Thread(target=_background_scan_loop, name="background-scan", daemon=True).start()
    last_signature = ""
    last_processed_symbols: dict[str, float] = {}
    last_maintenance_day = ""
    while True:
        config = load_config()
        state = load_state()
        today = datetime.now(timezone.utc).date().isoformat()
        if today != last_maintenance_day:
            try:
                maintain_telemetry(int(config.get("telemetry_retention_days", 30)))
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
