from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Callable

from app.binance_client import BinanceFuturesClient
from app.binance_rate import BinanceRateLimitError, request_priority
from app.cross_sectional_momentum import _atr, _fresh
from app.market_stream import data_dir, read_snapshot
from app.shadow_trading import update_shadow_trades
from app.telemetry import record_event_throttled


STRATEGY_FAMILY = "adaptive_30d_momentum"
STRATEGY_VERSION = "s0_xmom_30d_paper_v1"
_THREAD: threading.Thread | None = None
_LOCK = threading.Lock()


def _status_path() -> Path:
    return data_dir() / "adaptive_30d_momentum_status.json"


def _write_status(payload: dict[str, Any]) -> None:
    path = _status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)


def adaptive_30d_momentum_status() -> dict[str, Any]:
    try:
        return json.loads(_status_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "enabled": False,
            "strategy_family": STRATEGY_FAMILY,
            "strategy_version": STRATEGY_VERSION,
            "status": "waiting",
            "reason": "等待第一次独立未来影子评估",
        }


def _inside_daily_window(config: dict[str, Any], now: datetime) -> bool:
    hour = int(config.get("adaptive_30d_shadow_utc_hour", 0))
    min_minute = int(config.get("adaptive_30d_shadow_minute_start", 2))
    max_minute = int(config.get("adaptive_30d_shadow_minute_end", 45))
    return now.hour == hour and min_minute <= now.minute <= max_minute


def _eligible_symbols(
    snapshot: dict[str, Any],
    exchange_info: dict[str, Any],
    config: dict[str, Any],
    now: datetime,
) -> list[dict[str, Any]]:
    min_volume = float(config.get("adaptive_30d_shadow_min_24h_volume_usdt", 20_000_000))
    minimum_age_days = int(config.get("adaptive_30d_shadow_min_onboard_age_days", 45))
    max_age = int(config.get("adaptive_30d_shadow_stream_max_age_seconds", 30))
    limit = int(config.get("adaptive_30d_shadow_symbol_limit", 150))
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
            price = float(ticker.get("lastPrice") or 0)
            quote_volume = float(ticker.get("quoteVolume") or 0)
            onboard_ms = int(info.get("onboardDate") or 0)
        except (TypeError, ValueError):
            continue
        age_days = (now.timestamp() * 1000 - onboard_ms) / 86_400_000 if onboard_ms > 0 else 0
        if price <= 0 or quote_volume < min_volume or age_days < minimum_age_days:
            continue
        rows.append(
            {
                "symbol": upper,
                "last_price": price,
                "quote_volume": quote_volume,
                "symbol_age_days": age_days,
            }
        )
    rows.sort(key=lambda item: item["quote_volume"], reverse=True)
    return rows[: max(1, limit)]


def completed_hourly_metrics(rows: list[list[Any]], boundary_ms: int) -> dict[str, float] | None:
    completed = [row for row in rows if row and int(row[6]) < boundary_ms]
    completed.sort(key=lambda row: int(row[0]))
    if len(completed) < 721:
        return None
    scoped = completed[-721:]
    if any(int(scoped[index][0]) - int(scoped[index - 1][0]) != 3_600_000 for index in range(1, len(scoped))):
        return None
    start = float(scoped[0][4])
    end = float(scoped[-1][4])
    atr = _atr(scoped, 24)
    if start <= 0 or end <= 0 or atr <= 0:
        return None
    return {"return_30d": end / start - 1.0, "close": end, "atr_24h": atr}


def _fetch_metrics(
    client: BinanceFuturesClient,
    symbols: list[str],
    boundary_ms: int,
    sleep_fn: Callable[[float], None],
) -> tuple[dict[str, dict[str, float]], int]:
    metrics: dict[str, dict[str, float]] = {}
    retries = 0
    for symbol in symbols:
        while True:
            try:
                with request_priority("background"):
                    rows = client.klines(symbol, "1h", 722)
                break
            except BinanceRateLimitError as exc:
                retries += 1
                sleep_fn(max(1.0, min(float(exc.retry_after or 5.0), 60.0)))
        value = completed_hourly_metrics(rows, boundary_ms)
        if value:
            metrics[symbol] = value
    return metrics, retries


def build_adaptive_30d_shadow_candidate(
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
        return None, {"status": "outside_daily_window", "reason": "等待每日固定 UTC 评估窗口"}

    exchange_info = client.exchange_info()
    universe = _eligible_symbols(snapshot, exchange_info, config, now)
    minimum_universe = int(config.get("adaptive_30d_shadow_min_universe", 60))
    if len(universe) < minimum_universe:
        return None, {
            "status": "insufficient_universe",
            "reason": f"高流动性且上市满 45 天的币种不足：{len(universe)}/{minimum_universe}",
            "universe_size": len(universe),
        }

    boundary = now.replace(minute=0, second=0, microsecond=0)
    boundary_ms = int(boundary.timestamp() * 1000)
    symbols = [row["symbol"] for row in universe]
    metrics, rate_retries = _fetch_metrics(client, ["BTCUSDT", *symbols], boundary_ms, sleep_fn)
    usable = [row for row in universe if row["symbol"] in metrics]
    if len(usable) < minimum_universe or "BTCUSDT" not in metrics:
        return None, {
            "status": "insufficient_history",
            "reason": f"连续 30 日小时数据不足：{len(usable)}/{minimum_universe}",
            "universe_size": len(universe),
            "usable_universe_size": len(usable),
            "rate_limit_retries": rate_retries,
        }

    returns = [metrics[row["symbol"]]["return_30d"] for row in usable]
    breadth = median(returns)
    btc_return = metrics["BTCUSDT"]["return_30d"]
    min_breadth = float(config.get("adaptive_30d_shadow_min_abs_breadth_pct", 2.0)) / 100
    max_breadth = float(config.get("adaptive_30d_shadow_max_abs_breadth_pct", 10.0)) / 100
    common = {
        "universe_size": len(universe),
        "usable_universe_size": len(usable),
        "breadth_30d_pct": round(breadth * 100, 6),
        "btc_return_30d_pct": round(btc_return * 100, 6),
        "rate_limit_retries": rate_retries,
        "signal_boundary": boundary.isoformat(),
    }
    if not min_breadth <= abs(breadth) <= max_breadth:
        return None, {
            **common,
            "status": "breadth_outside_band",
            "reason": f"30 日市场广度 {breadth * 100:.2f}% 不在 {min_breadth * 100:.1f}%-{max_breadth * 100:.1f}% 研究带",
        }
    if btc_return > 0 and breadth > 0:
        direction = "LONG"
        selected = max(usable, key=lambda row: metrics[row["symbol"]]["return_30d"])
        regime = "broad_up"
    elif btc_return < 0 and breadth < 0:
        direction = "SHORT"
        selected = min(usable, key=lambda row: metrics[row["symbol"]]["return_30d"])
        regime = "broad_down"
    else:
        return None, {
            **common,
            "status": "mixed_market",
            "reason": "BTC 与山寨币 30 日广度不同向，不建立研究影子",
        }

    latest = read_snapshot()
    live_ticker = (latest.get("tickers") or {}).get(selected["symbol"]) or {}
    entry = float(live_ticker.get("lastPrice") or selected["last_price"])
    metric = metrics[selected["symbol"]]
    stop_atr = float(config.get("adaptive_30d_shadow_stop_atr", 2.5))
    reward_r = float(config.get("adaptive_30d_shadow_reward_r", 2.5))
    max_stop_pct = float(config.get("adaptive_30d_shadow_max_stop_pct", 12.0))
    stop_distance = min(stop_atr * metric["atr_24h"], entry * max_stop_pct / 100)
    sign = 1 if direction == "LONG" else -1
    stop = entry - sign * stop_distance
    take = entry + sign * stop_distance * reward_r
    day = boundary.date().isoformat()
    context = {
        **common,
        "symbol": selected["symbol"],
        "direction": direction,
        "market_regime": regime,
        "selected_return_30d_pct": round(metric["return_30d"] * 100, 6),
        "symbol_age_days": round(selected["symbol_age_days"], 3),
        "quote_volume_24h": round(selected["quote_volume"], 2),
        "entry_delay_seconds": max(0, int((now - boundary).total_seconds())),
        "gate_policy": "ungated_future_paper_all_candidates",
    }
    hold_hours = int(config.get("adaptive_30d_shadow_max_hold_hours", 120))
    candidate = {
        "symbol": selected["symbol"],
        "direction": direction,
        "entry_type": "adaptive_xmom_30d",
        "mode": "research_shadow",
        "strategy": "adaptive_30d_momentum_shadow",
        "strategy_family": STRATEGY_FAMILY,
        "strategy_version": STRATEGY_VERSION,
        "strategy_role": "challenger",
        "strategy_generation": "independent-future-paper",
        "evidence_type": "independent_realtime",
        "shadow_force_eligible": True,
        "shadow_single_position": True,
        "shadow_max_hold_minutes": hold_hours * 60,
        "shadow_dedupe_key": f"{STRATEGY_VERSION}:{day}",
        "score": 100.0,
        "passed": False,
        "decision_reason": "独立 30 日动量未来影子，只收集研究证据，绝不参与实盘准入或仓位",
        "market_state": {"state": regime},
        "ticker": {"last": entry},
        "signal": {
            "signal": direction,
            "last_price": entry,
            "atr": metric["atr_24h"],
            "stop": stop,
            "take_profit": take,
            "protection_profile": {
                "stop_atr": stop_atr,
                "take_profit_atr": stop_atr * reward_r,
                "max_hold_seconds": hold_hours * 3600,
            },
        },
        "opportunity_id": f"{STRATEGY_VERSION}:{day}",
        "event_id": f"{STRATEGY_VERSION}:{day}",
        "parameter_fingerprint": (
            f"{STRATEGY_VERSION}:formation=720:cadence=24:breadth=2-10:"
            f"stop={stop_atr}:r={reward_r}:hold={hold_hours}:cost="
            f"{float(config.get('shadow_round_trip_cost_pct', 0.12))}"
        ),
        "research_context": context,
    }
    return candidate, {**context, "status": "candidate_ready", "atr_24h": round(metric["atr_24h"], 10)}


def _run(config_provider: Callable[[], dict[str, Any]]) -> None:
    evaluated_day = ""
    while True:
        try:
            config = config_provider()
            enabled = bool(config.get("adaptive_30d_shadow_enabled", True))
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
                candidate, status = build_adaptive_30d_shadow_candidate(
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
                "adaptive_30d_shadow",
                "30 日动量未来影子评估失败，实盘不受影响。",
                {"error": str(exc)},
                throttle_seconds=300,
            )
        time.sleep(10)


def start_adaptive_30d_momentum_thread(config_provider: Callable[[], dict[str, Any]]) -> None:
    global _THREAD
    with _LOCK:
        if _THREAD and _THREAD.is_alive():
            return
        _THREAD = threading.Thread(
            target=_run,
            args=(config_provider,),
            name="adaptive-30d-research-shadow",
            daemon=True,
        )
        _THREAD.start()
