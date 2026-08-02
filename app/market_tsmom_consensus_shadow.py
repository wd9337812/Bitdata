from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Callable

from app.binance_client import BinanceFuturesClient
from app.binance_rate import BinanceRateLimitError, request_priority
from app.cross_sectional_momentum import _fresh
from app.market_stream import data_dir, read_snapshot
from app.shadow_trading import update_shadow_trades
from app.telemetry import record_event_throttled


STRATEGY_FAMILY = "market_tsmom_consensus"
STRATEGY_VERSION = "s0_market_tsmom_28_56_v1"
_THREAD: threading.Thread | None = None
_LOCK = threading.Lock()


def _status_path() -> Path:
    return data_dir() / "market_tsmom_consensus_status.json"


def _write_status(payload: dict[str, Any]) -> None:
    path = _status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def market_tsmom_consensus_status() -> dict[str, Any]:
    try:
        return json.loads(_status_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "enabled": False,
            "strategy_family": STRATEGY_FAMILY,
            "strategy_version": STRATEGY_VERSION,
            "status": "waiting",
            "reason": "等待第一次 28/56 日市场趋势共振评估",
        }


def _inside_daily_window(config: dict[str, Any], now: datetime) -> bool:
    hour = int(config.get("market_tsmom_shadow_utc_hour", 0))
    start = int(config.get("market_tsmom_shadow_minute_start", 3))
    end = int(config.get("market_tsmom_shadow_minute_end", 50))
    return now.hour == hour and start <= now.minute <= end


def _eligible_symbols(
    snapshot: dict[str, Any],
    exchange_info: dict[str, Any],
    config: dict[str, Any],
    now: datetime,
) -> list[dict[str, Any]]:
    min_volume = float(config.get("market_tsmom_shadow_min_24h_volume_usdt", 20_000_000))
    minimum_age_days = int(config.get("market_tsmom_shadow_min_onboard_age_days", 90))
    max_age = int(config.get("market_tsmom_shadow_stream_max_age_seconds", 30))
    pool_size = int(config.get("market_tsmom_shadow_prefetch_symbols", 40))
    exchange_symbols = {
        str(item.get("symbol") or "").upper(): item
        for item in exchange_info.get("symbols", [])
        if str(item.get("status") or "") == "TRADING"
        and str(item.get("contractType") or "") == "PERPETUAL"
        and str(item.get("quoteAsset") or "") == "USDT"
    }
    rows: list[dict[str, Any]] = []
    for symbol, ticker in (snapshot.get("tickers") or {}).items():
        upper = str(symbol).upper()
        info = exchange_symbols.get(upper)
        if not info or upper in {"BTCUSDT", "ETHUSDT"} or not _fresh(ticker, now, max_age):
            continue
        try:
            quote_volume = float(ticker.get("quoteVolume") or 0)
            onboard_ms = int(info.get("onboardDate") or 0)
        except (TypeError, ValueError):
            continue
        age_days = (now.timestamp() * 1000 - onboard_ms) / 86_400_000 if onboard_ms > 0 else 0
        if quote_volume < min_volume or age_days < minimum_age_days:
            continue
        rows.append(
            {
                "symbol": upper,
                "quote_volume": quote_volume,
                "symbol_age_days": age_days,
            }
        )
    rows.sort(key=lambda item: item["quote_volume"], reverse=True)
    return rows[: max(20, pool_size)]


def completed_daily_series(
    rows: list[list[Any]], boundary_ms: int, minimum_bars: int = 57
) -> dict[str, Any] | None:
    completed = [row for row in rows if row and int(row[6]) < boundary_ms]
    completed.sort(key=lambda row: int(row[0]))
    if len(completed) < minimum_bars:
        return None
    scoped = completed[-minimum_bars:]
    if any(
        int(scoped[index][0]) - int(scoped[index - 1][0]) != 86_400_000
        for index in range(1, len(scoped))
    ):
        return None
    closes = [float(row[4]) for row in scoped]
    quote_volumes = [float(row[7]) for row in scoped]
    if any(value <= 0 for value in closes):
        return None
    returns = [closes[index] / closes[index - 1] - 1.0 for index in range(1, len(closes))]
    return {
        "returns": returns,
        "median_quote_volume_30d": median(quote_volumes[-30:]),
        "last_close": closes[-1],
    }


def market_consensus_metrics(
    series: dict[str, dict[str, Any]], top_symbols: int = 20
) -> dict[str, Any] | None:
    usable = sorted(
        series.items(),
        key=lambda item: float(item[1]["median_quote_volume_30d"]),
        reverse=True,
    )[:top_symbols]
    if len(usable) < top_symbols:
        return None
    market_returns = [
        sum(float(item[1]["returns"][index]) for item in usable) / len(usable)
        for index in range(56)
    ]
    momentum_28d = math.prod(1.0 + value for value in market_returns[-28:]) - 1.0
    momentum_56d = math.prod(1.0 + value for value in market_returns) - 1.0
    return {
        "momentum_28d": momentum_28d,
        "momentum_56d": momentum_56d,
        "symbols": [symbol for symbol, _ in usable],
        "median_volume_floor": min(
            float(item[1]["median_quote_volume_30d"]) for item in usable
        ),
    }


def _fetch_daily_series(
    client: BinanceFuturesClient,
    symbols: list[str],
    boundary_ms: int,
    sleep_fn: Callable[[float], None],
) -> tuple[dict[str, dict[str, Any]], int]:
    result: dict[str, dict[str, Any]] = {}
    retries = 0
    for symbol in symbols:
        while True:
            try:
                with request_priority("background"):
                    rows = client.klines(symbol, "1d", 60)
                break
            except BinanceRateLimitError as exc:
                retries += 1
                sleep_fn(max(1.0, min(float(exc.retry_after or 5.0), 60.0)))
        value = completed_daily_series(rows, boundary_ms)
        if value:
            result[symbol] = value
    return result, retries


def build_market_tsmom_shadow_candidate(
    client: BinanceFuturesClient,
    snapshot: dict[str, Any],
    config: dict[str, Any],
    now: datetime,
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if not _inside_daily_window(config, now):
        return None, {"status": "outside_daily_window", "reason": "等待每日 UTC 固定评估窗口"}

    universe = _eligible_symbols(snapshot, client.exchange_info(), config, now)
    prefetch_minimum = int(config.get("market_tsmom_shadow_min_prefetch_symbols", 25))
    if len(universe) < prefetch_minimum:
        return None, {
            "status": "insufficient_universe",
            "reason": f"高流动性且币龄合格的预选币不足：{len(universe)}/{prefetch_minimum}",
            "universe_size": len(universe),
        }

    boundary = now.replace(hour=0, minute=0, second=0, microsecond=0)
    boundary_ms = int(boundary.timestamp() * 1000)
    series, retries = _fetch_daily_series(
        client, [item["symbol"] for item in universe], boundary_ms, sleep_fn
    )
    top_symbols = int(config.get("market_tsmom_shadow_market_symbols", 20))
    metrics = market_consensus_metrics(series, top_symbols)
    if metrics is None:
        return None, {
            "status": "insufficient_history",
            "reason": f"连续日线数据不足：{len(series)}/{top_symbols}",
            "universe_size": len(universe),
            "usable_universe_size": len(series),
            "rate_limit_retries": retries,
        }

    threshold = float(config.get("market_tsmom_shadow_top_third_threshold_pct", 10.65)) / 100
    context = {
        "status": "no_signal",
        "universe_size": len(universe),
        "usable_universe_size": len(series),
        "market_symbols": len(metrics["symbols"]),
        "market_momentum_28d_pct": round(metrics["momentum_28d"] * 100, 6),
        "market_momentum_56d_pct": round(metrics["momentum_56d"] * 100, 6),
        "top_third_threshold_pct": round(threshold * 100, 6),
        "median_volume_floor_usdt": round(metrics["median_volume_floor"], 2),
        "rate_limit_retries": retries,
        "signal_boundary": boundary.isoformat(),
        "market_basket": metrics["symbols"],
    }
    if not (metrics["momentum_28d"] > threshold and metrics["momentum_56d"] > 0):
        return None, {
            **context,
            "reason": "28 日市场动量未超过历史上三分位，或 56 日慢趋势未转正",
        }

    ticker = (read_snapshot().get("tickers") or {}).get("BTCUSDT") or (
        snapshot.get("tickers") or {}
    ).get("BTCUSDT") or {}
    entry = float(ticker.get("lastPrice") or 0)
    if entry <= 0:
        return None, {**context, "status": "missing_btc_price", "reason": "BTC 实时价格不可用"}
    stop_pct = float(config.get("market_tsmom_shadow_stop_pct", 10.0)) / 100
    hold_hours = int(config.get("market_tsmom_shadow_max_hold_hours", 120))
    risk_pct = float(config.get("market_tsmom_shadow_reference_risk_pct", 15.0))
    day = boundary.date().isoformat()
    candidate = {
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "entry_type": "market_tsmom_28_56",
        "mode": "research_shadow",
        "strategy": "market_tsmom_consensus_shadow",
        "strategy_family": STRATEGY_FAMILY,
        "strategy_version": STRATEGY_VERSION,
        "strategy_role": "challenger",
        "strategy_generation": "public-research-fast-slow-consensus",
        "evidence_type": "independent_realtime",
        "shadow_force_eligible": True,
        "shadow_single_position": True,
        "shadow_disable_take_profit": True,
        "shadow_max_hold_minutes": hold_hours * 60,
        "shadow_dedupe_key": f"{STRATEGY_VERSION}:{day}",
        "score": 100.0,
        "passed": False,
        "decision_reason": "28/56 日市场趋势共振独立影子，只验证未来表现，不影响当前实盘",
        "market_state": {"state": "broad_up"},
        "ticker": {"last": entry},
        "signal": {
            "signal": "LONG",
            "last_price": entry,
            "stop": entry * (1.0 - stop_pct),
            "take_profit": entry * 10.0,
            "protection_profile": {
                "stop_pct": stop_pct * 100,
                "take_profit_mode": "none_time_exit_only",
                "max_hold_seconds": hold_hours * 3600,
            },
        },
        "opportunity_id": f"{STRATEGY_VERSION}:{day}",
        "event_id": f"{STRATEGY_VERSION}:{day}",
        "parameter_fingerprint": (
            f"{STRATEGY_VERSION}:fast=28:slow=56:threshold={threshold:.6f}:"
            f"stop={stop_pct:.4f}:hold={hold_hours}:risk={risk_pct:.2f}"
        ),
        "research_context": {
            **context,
            "status": "candidate_ready",
            "symbol": "BTCUSDT",
            "direction": "LONG",
            "reference_risk_pct": risk_pct,
            "max_risk_cap_pct": 30.0,
            "gate_policy": "isolated_shadow_no_live_effect",
        },
    }
    return candidate, candidate["research_context"]


def _run(config_provider: Callable[[], dict[str, Any]]) -> None:
    evaluated_day = ""
    while True:
        try:
            config = config_provider()
            enabled = bool(config.get("market_tsmom_shadow_enabled", True))
            now = datetime.now(timezone.utc)
            day = now.date().isoformat()
            base = {
                "enabled": enabled,
                "strategy_family": STRATEGY_FAMILY,
                "strategy_version": STRATEGY_VERSION,
                "updated_at": now.isoformat(),
                "live_effect": "none",
            }
            if not enabled:
                _write_status({**base, "status": "disabled", "reason": "配置已关闭"})
            elif day != evaluated_day:
                candidate, status = build_market_tsmom_shadow_candidate(
                    BinanceFuturesClient(
                        str(config.get("api_key") or ""),
                        str(config.get("api_secret") or ""),
                        str(config.get("binance_base_url") or "https://fapi.binance.com"),
                    ),
                    read_snapshot(),
                    config,
                    now,
                )
                if status.get("status") != "outside_daily_window":
                    evaluated_day = day
                    result = update_shadow_trades([candidate], config) if candidate else None
                    _write_status({**base, **status, "shadow_result": result})
                else:
                    _write_status({**base, **status})
        except Exception as exc:
            _write_status(
                {
                    "enabled": True,
                    "strategy_family": STRATEGY_FAMILY,
                    "strategy_version": STRATEGY_VERSION,
                    "status": "error",
                    "reason": str(exc),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "live_effect": "none",
                }
            )
            record_event_throttled(
                "warning",
                "market_tsmom_shadow",
                "28/56 日趋势共振影子评估失败，当前实盘不受影响。",
                {"error": str(exc)},
                throttle_seconds=300,
            )
        time.sleep(10)


def start_market_tsmom_consensus_thread(
    config_provider: Callable[[], dict[str, Any]],
) -> None:
    global _THREAD
    with _LOCK:
        if _THREAD and _THREAD.is_alive():
            return
        _THREAD = threading.Thread(
            target=_run,
            args=(config_provider,),
            name="market-tsmom-consensus-shadow",
            daemon=True,
        )
        _THREAD.start()
