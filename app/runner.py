from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from app.binance_client import BinanceFuturesClient
from app.config_store import load_config
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
    cooldowns = dict(state.get("symbol_cooldowns") or {})
    cooldowns[symbol.upper()] = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
    save_state({"symbol_cooldowns": cooldowns})


def synthetic_account(equity: float = 50.0) -> dict:
    return {"equity": equity, "available_balance": equity, "unrealized_pnl": 0.0, "positions": []}


def private_api_error(exc: Exception) -> str:
    return (
        "Binance 私有接口鉴权失败，请检查 API Key/Secret、U 本位合约权限、IP 白名单和系统时间。"
        f" 原始错误：{exc}"
    )


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
    if result.get("mode") == "live" and decision.get("symbol"):
        set_symbol_cooldown(state, decision["symbol"], float(config.get("symbol_cooldown_minutes", 15)))
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
    interval_seconds = int(os.getenv("BOT_LOOP_SECONDS", "300"))
    while True:
        try:
            result = run_once()
            interval_seconds = int(result.get("loop_seconds") or interval_seconds)
            print(result, flush=True)
        except Exception as exc:
            save_state({"last_error": str(exc), "bot_status": "paused"})
            record_event("error", "runner", str(exc))
            print({"status": "error", "error": str(exc)}, flush=True)
        time.sleep(interval_seconds)


if __name__ == "__main__":
    main()
