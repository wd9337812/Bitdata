from __future__ import annotations

import os
import secrets
import time
from pathlib import Path
from typing import Any

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from app.binance_client import BinanceFuturesClient
from app.config_store import load_config, save_config
from app.live_learning import list_live_scores, sync_live_learning_from_binance
from app.models import BotControlPayload, ExecutePayload, TradingConfig
from app.market_stream import stream_status
from app.state_store import load_state, save_state
from app.strategy import StrategyParams, backtest, latest_signal
from app.binance_rate import cache_status, rate_status
from app.telemetry import (
    heartbeat,
    latest_strategy_payload,
    list_equity_snapshots,
    list_events,
    list_strategy_runs,
    record_equity_snapshot,
    record_event,
)
from app.trading_engine import (
    build_best_growth_decision,
    build_grid_decisions,
    build_stage1_decision,
    execute_grid_orders,
    execute_stage1_market_order,
    summarize_account,
    sync_stage,
)

load_dotenv()

APP_DIR = Path(__file__).resolve().parent
app = FastAPI(title="Binance Futures Strategy Dashboard", version="0.1.0")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
assets_dir = APP_DIR / "static" / "assets"
if assets_dir.exists():
    app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")
security = HTTPBasic(auto_error=False)


def require_auth(credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
    user = os.getenv("BASIC_AUTH_USER", "")
    password = os.getenv("BASIC_AUTH_PASSWORD", "")
    if not user and not password:
        return
    if credentials is None:
        raise HTTPException(status_code=401, detail="Authentication required", headers={"WWW-Authenticate": "Basic"})
    valid_user = secrets.compare_digest(credentials.username, user)
    valid_password = secrets.compare_digest(credentials.password, password)
    if not (valid_user and valid_password):
        raise HTTPException(status_code=401, detail="Authentication required", headers={"WWW-Authenticate": "Basic"})


def client_from_config(include_secret: bool = True) -> BinanceFuturesClient:
    config = load_config(include_secret=include_secret)
    return BinanceFuturesClient(
        api_key=config.get("api_key", ""),
        api_secret=config.get("api_secret", ""),
        base_url=config.get("binance_base_url", "https://fapi.binance.com"),
    )


def synthetic_account(equity: float = 50.0) -> dict[str, Any]:
    return {"equity": equity, "available_balance": equity, "unrealized_pnl": 0.0, "positions": []}


def private_api_error(exc: Exception) -> str:
    return (
        "Binance 私有接口鉴权失败，请检查 API Key/Secret、U 本位合约权限、IP 白名单和系统时间。"
        f" 原始错误：{exc}"
    )


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def index() -> str:
    return (APP_DIR / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/api/config", dependencies=[Depends(require_auth)])
def get_config() -> dict[str, Any]:
    return load_config(include_secret=False)


@app.post("/api/config", dependencies=[Depends(require_auth)])
def post_config(payload: TradingConfig) -> dict[str, Any]:
    return save_config(payload.model_dump())


@app.post("/api/config/test-binance", dependencies=[Depends(require_auth)])
def test_binance_config() -> dict[str, Any]:
    config = load_config()
    if not config.get("api_key") or not config.get("api_secret"):
        raise HTTPException(status_code=400, detail="请先填写 Binance API Key 和 Secret 并保存。")
    try:
        account = summarize_account(client_from_config().account())
        return {"ok": True, "account": account}
    except Exception as exc:
        error = private_api_error(exc)
        record_event("error", "binance_auth", error)
        raise HTTPException(status_code=400, detail=error) from exc


@app.get("/api/status", dependencies=[Depends(require_auth)])
def status() -> dict[str, Any]:
    config = load_config()
    state = load_state()
    account_summary = {"equity": None, "available_balance": None, "unrealized_pnl": None, "positions": []}
    latest_snapshot = list_equity_snapshots(1)
    if latest_snapshot:
        snap = latest_snapshot[-1]
        account_summary = {
            "equity": snap.get("equity"),
            "available_balance": snap.get("available_balance"),
            "unrealized_pnl": snap.get("unrealized_pnl"),
            "positions": [],
        }
    return {
        "config": load_config(include_secret=False),
        "state": state,
        "account": account_summary,
        "binance_rate": rate_status(),
        "cache": cache_status(),
        "market_stream": stream_status(),
    }


@app.get("/api/equity/snapshots", dependencies=[Depends(require_auth)])
def equity_snapshots(limit: int = 500) -> dict[str, Any]:
    return {"snapshots": list_equity_snapshots(limit)}


@app.post("/api/equity/snapshot", dependencies=[Depends(require_auth)])
def equity_snapshot() -> dict[str, Any]:
    config = load_config()
    state = load_state()
    account_summary = {"equity": None, "available_balance": None, "unrealized_pnl": None, "positions": []}
    if config.get("api_key") and config.get("api_secret"):
        try:
            account_summary = summarize_account(client_from_config().account())
        except Exception as exc:
            error = private_api_error(exc)
            record_event("error", "binance_auth", error)
            state = save_state({"last_error": error})
    record_equity_snapshot(account_summary, state, action="manual_snapshot", reason="dashboard")
    return {"ok": True}


@app.get("/api/logs", dependencies=[Depends(require_auth)])
def logs(limit: int = 200, category: str | None = None) -> dict[str, Any]:
    return {"events": list_events(limit, category)}


@app.get("/api/strategy-runs", dependencies=[Depends(require_auth)])
def strategy_runs(limit: int = 200) -> dict[str, Any]:
    return {"runs": list_strategy_runs(limit)}


@app.get("/api/live-learning", dependencies=[Depends(require_auth)])
def live_learning(limit: int = 100) -> dict[str, Any]:
    return {"scores": list_live_scores(limit, load_config())}


@app.post("/api/live-learning/sync", dependencies=[Depends(require_auth)])
def sync_live_learning() -> dict[str, Any]:
    config = load_config()
    if not config.get("api_key") or not config.get("api_secret"):
        raise HTTPException(status_code=400, detail="请先配置 Binance API Key 和 Secret。")
    try:
        return sync_live_learning_from_binance(client_from_config(), config)
    except Exception as exc:
        record_event("error", "live_learning", str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/runner/heartbeat", dependencies=[Depends(require_auth)])
def runner_heartbeat() -> dict[str, Any]:
    return heartbeat()


@app.get("/api/health/binance", dependencies=[Depends(require_auth)])
def binance_health() -> dict[str, Any]:
    try:
        client = client_from_config()
        data = client.server_time()
        server_time = int(data.get("serverTime", 0))
        local_time = int(time.time() * 1000)
        offset_ms = local_time - server_time
        return {
            "ok": abs(offset_ms) < 3000,
            "serverTime": server_time,
            "localTime": local_time,
            "offsetMs": offset_ms,
            "warning": "VPS 时间偏差过大，请检查 chrony/NTP。" if abs(offset_ms) >= 3000 else "",
        }
    except Exception as exc:
        record_event("error", "binance", str(exc))
        return {"ok": False, "error": str(exc)}


@app.post("/api/control", dependencies=[Depends(require_auth)])
def control(payload: BotControlPayload) -> dict[str, Any]:
    action = payload.action.lower()
    if action == "start":
        if payload.confirmation != "START_BOT":
            raise HTTPException(status_code=400, detail="Use confirmation START_BOT.")
        config = load_config()
        live_mode = (
            not config.get("dry_run", True)
            and config.get("live_trading_enabled") is True
            and config.get("live_trading_confirmation") == "ENABLE_LIVE_TRADING"
        )
        if live_mode:
            try:
                client = client_from_config()
                offset_ms = client.time_offset_ms()
                if abs(offset_ms) >= 3000:
                    raise HTTPException(status_code=400, detail=f"VPS 时间偏差过大：{offset_ms}ms，请先同步时间。")
                client.account()
            except Exception as exc:
                if isinstance(exc, HTTPException):
                    raise exc
                error = private_api_error(exc)
                record_event("error", "binance_auth", error)
                raise HTTPException(status_code=400, detail=error) from exc
        record_event("warning", "control", "用户启动机器人")
        return {"state": save_state({"bot_status": "running", "last_error": ""})}
    if action == "pause":
        record_event("warning", "control", "用户暂停机器人")
        return {"state": save_state({"bot_status": "paused"})}
    if action == "stage_grid":
        if payload.confirmation != "SWITCH_TO_GRID":
            raise HTTPException(status_code=400, detail="Use confirmation SWITCH_TO_GRID.")
        return {"state": save_state({"stage": "grid", "bot_status": "paused"})}
    if action == "stage_growth":
        return {"state": save_state({"stage": "growth", "bot_status": "paused"})}
    raise HTTPException(status_code=400, detail="Unknown action.")


@app.get("/api/market", dependencies=[Depends(require_auth)])
def market() -> dict[str, Any]:
    config = load_config()
    symbols = [symbol.upper() for symbol in config["symbols"]]
    client = client_from_config()
    tickers = client.ticker_24h(symbols)
    funding = {item["symbol"]: item for item in client.premium_index(symbols)}
    rows = []
    for ticker in tickers:
        symbol = ticker["symbol"]
        rows.append(
            {
                "symbol": symbol,
                "last": float(ticker["lastPrice"]),
                "change_pct": float(ticker["priceChangePercent"]),
                "volume_usdt_b": round(float(ticker["quoteVolume"]) / 1_000_000_000, 3),
                "funding_pct": round(float(funding.get(symbol, {}).get("lastFundingRate", 0)) * 100, 5),
            }
        )
    return {"symbols": rows}


@app.get("/api/backtest", dependencies=[Depends(require_auth)])
def api_backtest(symbol: str | None = None) -> dict[str, Any]:
    config = load_config()
    symbols = [symbol.upper()] if symbol else [item.upper() for item in config["symbols"]]
    client = client_from_config()
    results = []
    for item in symbols:
        bars = client.klines(item, config["interval"], int(config["limit"]))
        results.append(backtest(item, bars, StrategyParams()))
    return {"results": results}


@app.get("/api/signals", dependencies=[Depends(require_auth)])
def api_signals() -> dict[str, Any]:
    config = load_config()
    client = client_from_config()
    signals = []
    for symbol in config["symbols"]:
        bars = client.klines(symbol.upper(), config["interval"], int(config["limit"]))
        signals.append(latest_signal(symbol.upper(), bars, StrategyParams()))
    return {"signals": signals}


@app.get("/api/decisions", dependencies=[Depends(require_auth)])
def api_decisions() -> dict[str, Any]:
    config = load_config()
    state = load_state()
    account_summary = {"equity": None, "available_balance": None, "unrealized_pnl": None, "positions": []}
    auth_error = ""
    latest = latest_strategy_payload()
    payload = (latest or {}).get("payload") or {}
    best_growth = (payload.get("decision") or {}) if payload else {}
    scan = best_growth.get("scan") or {}
    if latest:
        account_summary.update(
            {
                "equity": latest.get("equity"),
                "available_balance": None,
                "unrealized_pnl": None,
                "positions": [],
            }
        )
    top_candidates = scan.get("candidates", [])[:10]
    stage1_decisions = top_candidates or ([best_growth] if best_growth else [])
    return {
        "state": state,
        "account": account_summary,
        "auth_error": auth_error,
        "stage1": stage1_decisions,
        "growth_scan": scan,
        "stage2_grid": [],
        "source": "runner_latest",
        "latest_run": {key: latest.get(key) for key in ["id", "ts", "action", "symbol", "reason"]} if latest else None,
        "binance_rate": rate_status(),
    }


@app.get("/api/account", dependencies=[Depends(require_auth)])
def api_account() -> dict[str, Any]:
    config = load_config()
    if not config.get("api_key") or not config.get("api_secret"):
        raise HTTPException(status_code=400, detail="API key and secret are not configured.")
    account = client_from_config().account()
    return {
        "total_wallet_balance": account.get("totalWalletBalance"),
        "available_balance": account.get("availableBalance"),
        "total_unrealized_profit": account.get("totalUnrealizedProfit"),
        "assets": [
            asset for asset in account.get("assets", []) if float(asset.get("walletBalance", 0)) != 0
        ],
    }


@app.post("/api/execute", dependencies=[Depends(require_auth)])
def api_execute(payload: ExecutePayload) -> dict[str, Any]:
    config = load_config()
    live_allowed = (
        not config.get("dry_run", True)
        and config.get("live_trading_enabled") is True
        and config.get("live_trading_confirmation") == "ENABLE_LIVE_TRADING"
    )
    if not live_allowed:
        return {
            "mode": "dry_run",
            "message": "Order was simulated. Set dry_run=false, live_trading_enabled=true, and confirmation phrase to ENABLE_LIVE_TRADING to allow live orders.",
            "order": payload.model_dump(),
        }
    order = client_from_config().place_market_order(
        symbol=payload.symbol,
        side=payload.side,
        quantity=payload.quantity,
        reduce_only=payload.reduce_only,
    )
    return {"mode": "live", "order": order}


@app.post("/api/execute/stage1", dependencies=[Depends(require_auth)])
def api_execute_stage1(symbol: str | None = None) -> dict[str, Any]:
    config = load_config()
    state = load_state()
    client = client_from_config()
    if config.get("dry_run", True):
        account_summary = synthetic_account()
    elif config.get("api_key") and config.get("api_secret"):
        try:
            account_summary = summarize_account(client.account())
            state = sync_stage(config, state, account_summary)
        except Exception as exc:
            error = private_api_error(exc)
            record_event("error", "binance_auth", error)
            raise HTTPException(status_code=400, detail=error) from exc
    else:
        raise HTTPException(status_code=400, detail="实盘模式需要先配置 Binance API Key 和 Secret。")
    if symbol:
        bars = client.klines(symbol.upper(), config["interval"], int(config["limit"]))
        decision = build_stage1_decision(symbol.upper(), bars, config, state, account_summary)
    else:
        decision = build_best_growth_decision(client, config, state, account_summary)
    return execute_stage1_market_order(client, decision, config)


@app.post("/api/execute/grid", dependencies=[Depends(require_auth)])
def api_execute_grid(symbol: str) -> dict[str, Any]:
    config = load_config()
    state = load_state()
    if state.get("stage") != "grid":
        raise HTTPException(status_code=400, detail="Grid execution requires grid stage.")
    client = client_from_config()
    if config.get("dry_run", True):
        account_summary = synthetic_account(10000.0)
    elif config.get("api_key") and config.get("api_secret"):
        try:
            account_summary = summarize_account(client.account())
            state = sync_stage(config, state, account_summary)
        except Exception as exc:
            error = private_api_error(exc)
            record_event("error", "binance_auth", error)
            raise HTTPException(status_code=400, detail=error) from exc
    else:
        raise HTTPException(status_code=400, detail="实盘模式需要先配置 Binance API Key 和 Secret。")
    bars = client.klines(symbol.upper(), config["interval"], int(config["limit"]))
    plan = build_grid_decisions(config, account_summary, {symbol.upper(): bars})[0]
    position_amount = 0.0
    for position in account_summary.get("positions", []):
        if position.get("symbol") == symbol.upper():
            position_amount = float(position.get("positionAmt", 0))
            break
    return execute_grid_orders(client, plan, config, position_amount=position_amount)


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", "8080")),
        reload=False,
    )
