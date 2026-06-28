from __future__ import annotations

import os
import time

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


def run_once() -> dict:
    config = load_config()
    state = load_state()
    client = BinanceFuturesClient(
        api_key=config.get("api_key", ""),
        api_secret=config.get("api_secret", ""),
        base_url=config.get("binance_base_url", "https://fapi.binance.com"),
    )
    if state.get("bot_status") != "running":
        return {"status": "paused"}

    if config.get("api_key") and config.get("api_secret"):
        account = summarize_account(client.account())
    else:
        account = {"equity": 50.0, "available_balance": 50.0, "unrealized_pnl": 0.0, "positions": []}
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
            results.append(execute_grid_orders(client, plan, config, position_amount=position_amount))
        return {"status": "grid_checked", "results": results}

    decision = build_best_growth_decision(client, config, state, account)
    result = execute_stage1_market_order(client, decision, config)
    return {"status": "growth_checked", "decision": decision, "results": [result]}


def main() -> None:
    load_dotenv()
    interval_seconds = int(os.getenv("BOT_LOOP_SECONDS", "300"))
    while True:
        try:
            print(run_once(), flush=True)
        except Exception as exc:
            save_state({"last_error": str(exc), "bot_status": "paused"})
            print({"status": "error", "error": str(exc)}, flush=True)
        time.sleep(interval_seconds)


if __name__ == "__main__":
    main()
