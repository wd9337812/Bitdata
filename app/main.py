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
from app.learning_report import latest_daily_learning_report, save_daily_learning_report
from app.live_learning import (
    EXTREME_V2_FAMILY,
    EXTREME_V3_FAMILY,
    ORDERBOOK_SCALP_FAMILY,
    list_live_scores,
    list_strategy_live_scores,
    sync_live_learning_from_binance,
)
from app.live_reaction import list_live_reactions, list_recent_live_reaction_trades, sync_live_reaction_from_binance
from app.models import BotControlPayload, ExecutePayload, TradingConfig
from app.market_stream import stream_status
from app.opportunity_queue import opportunity_status
from app.product_completion import product_completion_summary
from app.runtime_protection import manage_runtime_protection
from app.runtime_snapshot import read_runtime_snapshot
from app.scanner import mode_config
from app.shadow_trading import shadow_summary
from app.strategy_calibration import calibration_snapshot
from app.strategy_releases import list_strategy_releases
from app.stage_modes import all_stage_profiles, stage_profile_for_equity
from app.stage_simulation import simulate_stage_path
from app.state_store import load_state, save_state
from app.strategy import StrategyParams, backtest, latest_signal
from app.target import target_progress
from app.binance_rate import BinanceRateLimitError, cache_status, rate_status, request_priority
from app.telemetry import (
    heartbeat,
    latest_strategy_payload,
    list_equity_snapshots,
    list_events,
    list_strategy_runs,
    record_equity_snapshot,
    record_event,
    telemetry_storage_status,
)
from app.user_stream import user_stream_status
from app.v31_validation import compare_v31_to_plain_breakout
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
app = FastAPI(title="Binance Futures Strategy Dashboard", version="0.8.0")
_BINANCE_HEALTH_CACHE: dict[str, Any] = {}
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
    runtime = read_runtime_snapshot()
    runtime_status = {
        key: runtime.get(key)
        for key in ["updated_at", "age_seconds", "channel", "last_cycle", "fast_lane", "background_scan", "protection_audit", "risk_status", "shadow_trading"]
    }
    return {
        "config": load_config(include_secret=False),
        "state": state,
        "account": account_summary,
        "target_progress": target_progress(config, state, account_summary),
        "stage_profile": state.get("stage_route") or stage_profile_for_equity(account_summary.get("equity"), config),
        "stage_route": state.get("stage_route") or {},
        "stage_profiles": all_stage_profiles(config),
        "product_completion": product_completion_summary(config),
        "binance_rate": rate_status(),
        "cache": cache_status(),
        "market_stream": stream_status(),
        "user_stream": user_stream_status(),
        "opportunity_queue": opportunity_status(
            max_age_seconds=int(config.get("opportunity_queue_ttl_seconds", 240)),
            limit=int(config.get("opportunity_queue_scan_limit", 50)),
        ),
        "runtime": runtime_status,
        "storage": telemetry_storage_status(),
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
    return {"events": list_events(limit, category, include_payload=False)}


@app.get("/api/product/completion", dependencies=[Depends(require_auth)])
def product_completion() -> dict[str, Any]:
    return product_completion_summary(load_config())


@app.get("/api/simulation/stage", dependencies=[Depends(require_auth)])
def stage_simulation(start_equity: float | None = None, target_equity: float | None = None, days: int | None = None) -> dict[str, Any]:
    return simulate_stage_path(load_config(), start_equity=start_equity, target_equity=target_equity, days=days)


@app.get("/api/reports/latest", dependencies=[Depends(require_auth)])
def report_latest() -> dict[str, Any]:
    return latest_daily_learning_report()


@app.post("/api/reports/daily", dependencies=[Depends(require_auth)])
def report_daily() -> dict[str, Any]:
    return save_daily_learning_report()


@app.post("/api/protection/runtime-check", dependencies=[Depends(require_auth)])
def runtime_protection_check() -> dict[str, Any]:
    config = load_config()
    state = load_state()
    if config.get("dry_run", True):
        account = synthetic_account()
    else:
        account = summarize_account(client_from_config().account_live())
    return manage_runtime_protection(client_from_config(), config, state, account)


@app.get("/api/strategy-runs", dependencies=[Depends(require_auth)])
def strategy_runs(limit: int = 200) -> dict[str, Any]:
    return {"runs": list_strategy_runs(limit)}


@app.get("/api/shadow-trades", dependencies=[Depends(require_auth)])
def shadow_trades(limit: int = 100) -> dict[str, Any]:
    return shadow_summary(limit, load_config())


@app.get("/api/strategy-releases", dependencies=[Depends(require_auth)])
def strategy_releases() -> dict[str, Any]:
    return {"releases": list_strategy_releases(load_config())}


@app.get("/api/live-learning", dependencies=[Depends(require_auth)])
def live_learning(limit: int = 100) -> dict[str, Any]:
    config = load_config()
    return {
        "scores": list_live_scores(limit, config),
        "strategy_scores": list_strategy_live_scores(limit, config),
        "scalp_scores": list_strategy_live_scores(limit, config, strategy_family=ORDERBOOK_SCALP_FAMILY),
        "extreme_scores": list_strategy_live_scores(limit, config, strategy_family=EXTREME_V2_FAMILY),
        "v3_scores": list_strategy_live_scores(limit, config, strategy_family=EXTREME_V3_FAMILY),
        "v3_calibration": calibration_snapshot(config),
    }


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


@app.get("/api/live-reaction", dependencies=[Depends(require_auth)])
def live_reaction(limit: int = 100) -> dict[str, Any]:
    return {
        "reactions": list_live_reactions(limit),
        "recent_trades": list_recent_live_reaction_trades(50),
    }


@app.post("/api/live-reaction/sync", dependencies=[Depends(require_auth)])
def sync_live_reaction() -> dict[str, Any]:
    config = load_config()
    if not config.get("api_key") or not config.get("api_secret"):
        raise HTTPException(status_code=400, detail="请先配置 Binance API Key 和 Secret。")
    try:
        account = summarize_account(client_from_config().account_live()) if not config.get("dry_run", True) else synthetic_account()
        return sync_live_reaction_from_binance(
            client_from_config(),
            config,
            account=account,
            equity=float(account.get("equity") or 0),
        )
    except Exception as exc:
        record_event("error", "live_reaction", str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/runner/heartbeat", dependencies=[Depends(require_auth)])
def runner_heartbeat() -> dict[str, Any]:
    return heartbeat()


@app.get("/api/health/binance", dependencies=[Depends(require_auth)])
def binance_health() -> dict[str, Any]:
    now_monotonic = time.monotonic()
    cache_age = now_monotonic - float(_BINANCE_HEALTH_CACHE.get("checked_monotonic") or 0)
    if _BINANCE_HEALTH_CACHE.get("payload") and cache_age < 60:
        return {**_BINANCE_HEALTH_CACHE["payload"], "cached": True, "cacheAgeSeconds": round(cache_age, 2)}
    try:
        client = client_from_config()
        with request_priority("background"):
            data = client.server_time()
        server_time = int(data.get("serverTime", 0))
        local_time = int(time.time() * 1000)
        offset_ms = local_time - server_time
        payload = {
            "ok": abs(offset_ms) < 3000,
            "serverTime": server_time,
            "localTime": local_time,
            "offsetMs": offset_ms,
            "warning": "VPS 时间偏差过大，请检查 chrony/NTP。" if abs(offset_ms) >= 3000 else "",
        }
        _BINANCE_HEALTH_CACHE.update({"checked_monotonic": now_monotonic, "payload": payload})
        return payload
    except BinanceRateLimitError as exc:
        if _BINANCE_HEALTH_CACHE.get("payload"):
            return {
                **_BINANCE_HEALTH_CACHE["payload"],
                "cached": True,
                "cacheAgeSeconds": round(cache_age, 2),
                "warning": "REST 预算优先保留给交易，当前显示最近一次健康结果。",
            }
        payload = {
            "ok": True,
            "deferred": True,
            "source": "stream_and_rate_state",
            "warning": "REST 预算优先保留给交易；当前由实时行情流和频控状态确认连接正常。",
        }
        _BINANCE_HEALTH_CACHE.update({"checked_monotonic": now_monotonic, "payload": payload})
        return payload
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
        state = load_state()
        start_updates: dict[str, Any] = {"bot_status": "running", "last_error": ""}
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
                account_summary = summarize_account(client.account())
                equity = account_summary.get("equity")
                if state.get("hard_stop_triggered"):
                    recovery = float(config.get("hard_stop_recovery_equity", 5.5))
                    if equity is None or float(equity) < recovery:
                        raise HTTPException(
                            status_code=400,
                            detail=f"账户曾触发硬停止，请先将合约权益补充到 {recovery:.2f}U 以上再手动启动。",
                        )
                    start_updates.update(
                        {
                            "hard_stop_triggered": False,
                            "hard_stop_reason": "",
                            "risk_warning_active": float(equity) < float(config.get("risk_warning_equity", 30.0)),
                        }
                    )
                if equity is not None and mode_config(config, equity).get("mode") == "extreme_sprint":
                    if state.get("equity_guard_mode") != "extreme_sprint" or float(state.get("extreme_sprint_equity_high_watermark") or 0) <= 0:
                        start_updates["extreme_sprint_start_equity"] = float(equity)
                        start_updates["extreme_sprint_equity_high_watermark"] = float(equity)
                    start_updates["equity_guard_mode"] = "extreme_sprint"
            except Exception as exc:
                if isinstance(exc, HTTPException):
                    raise exc
                error = private_api_error(exc)
                record_event("error", "binance_auth", error)
                raise HTTPException(status_code=400, detail=error) from exc
        record_event("warning", "control", "用户启动机器人")
        return {"state": save_state(start_updates)}
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
    runtime = read_runtime_snapshot()
    return {
        "symbols": runtime.get("market") or [],
        "source": "runner_snapshot",
        "updated_at": runtime.get("updated_at"),
        "age_seconds": runtime.get("age_seconds"),
    }


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


@app.get("/api/validation/v31", dependencies=[Depends(require_auth)])
def api_validation_v31(symbol: str = "SOLUSDT", days: int = 180) -> dict[str, Any]:
    """Manual-only validation. The runner and Dashboard polling never call this endpoint."""
    safe_days = max(30, min(int(days), 365))
    config = load_config()
    client = client_from_config()
    bars = client.klines_history(symbol.upper(), "1h", safe_days, warmup=200)
    cost_pct = max(
        float(config.get("shadow_round_trip_cost_pct", 0.12)),
        float(config.get("taker_fee_pct_round_trip", 0.08)) + float(config.get("estimated_slippage_pct", 0.04)),
    )
    return {"symbol": symbol.upper(), "days": safe_days, "bars": len(bars), **compare_v31_to_plain_breakout(bars, cost_pct)}


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
    runtime = read_runtime_snapshot()
    latest = latest_strategy_payload()
    payload = (latest or {}).get("payload") or {}
    best_growth = runtime.get("decision") or ((payload.get("decision") or {}) if payload else {})
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
        "source": "runner_snapshot" if runtime.get("decision") else "runner_latest",
        "runtime": runtime,
        "latest_run": {key: latest.get(key) for key in ["id", "ts", "action", "symbol", "reason"]} if latest else None,
        "binance_rate": rate_status(),
    }


@app.get("/api/dashboard/live", dependencies=[Depends(require_auth)])
def api_dashboard_live() -> dict[str, Any]:
    """Single local-only payload for the 10-second Dashboard refresh."""
    return {"status": status(), "decisions": api_decisions()}


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
