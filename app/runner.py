from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from app.binance_client import BinanceFuturesClient
from app.binance_rate import BinanceRateLimitError, rate_status
from app.config_store import load_config
from app.live_learning import sync_live_learning_from_binance
from app.market_stream import start_market_stream_thread
from app.risk import direction_cooldown_key
from app.trading_engine import (
    build_best_growth_decision,
    build_grid_decisions,
    execute_grid_orders,
    execute_stage1_market_order,
    summarize_account,
    sync_stage,
)
from app.state_store import load_state, save_state
from app.telemetry import record_equity_snapshot, record_event, record_strategy_run


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


def is_timestamp_error(exc: Exception) -> bool:
    message = str(exc)
    return "-1021" in message or "recvWindow" in message or "Timestamp for this request" in message


def run_once() -> dict:
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
        except Exception as exc:
            raise RuntimeError(private_api_error(exc)) from exc
    else:
        raise RuntimeError("实盘模式需要先配置 Binance API Key 和 Secret。")
    state = sync_stage(config, state, account)
    maybe_sync_live_learning(client, config, state)

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

    decision = build_best_growth_decision(client, config, state, account)
    result = execute_stage1_market_order(client, decision, config)
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
    record_event("info", "growth", "完成增长模式扫描", {"decision": decision, "result": result})
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


if __name__ == "__main__":
    main()
