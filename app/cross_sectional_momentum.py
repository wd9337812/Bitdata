from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.binance_client import BinanceFuturesClient
from app.market_stream import data_dir, read_snapshot
from app.shadow_trading import update_shadow_trades
from app.telemetry import record_event_throttled


STRATEGY_FAMILY = "cross_sectional_momentum"
STRATEGY_VERSION = "s0_xmom_24h_v3"
_THREAD: threading.Thread | None = None
_LOCK = threading.Lock()


def _status_path() -> Path:
    return data_dir() / "cross_sectional_momentum_status.json"


def _write_status(payload: dict[str, Any]) -> None:
    path = _status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)


def cross_sectional_momentum_status() -> dict[str, Any]:
    try:
        return json.loads(_status_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "enabled": False,
            "strategy_family": STRATEGY_FAMILY,
            "strategy_version": STRATEGY_VERSION,
            "status": "waiting",
            "reason": "等待第一次实时评估",
        }


def _fresh(item: dict[str, Any], now: datetime, max_age_seconds: int) -> bool:
    try:
        updated = datetime.fromisoformat(str(item.get("updated_at") or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return 0 <= (now - updated).total_seconds() <= max_age_seconds


def select_cross_sectional_signal(
    snapshot: dict[str, Any],
    config: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    delay_seconds = now.minute * 60 + now.second
    min_delay = int(config.get("xmom_shadow_min_delay_seconds", 60))
    max_delay = int(config.get("xmom_shadow_max_delay_seconds", 150))
    if delay_seconds < min_delay or delay_seconds > max_delay:
        return {
            "status": "outside_entry_window",
            "reason": f"等待整点后 {min_delay}-{max_delay} 秒评估",
            "execution_delay_seconds": delay_seconds,
        }

    min_volume = float(config.get("xmom_shadow_min_24h_volume_usdt", 5_000_000))
    max_age = int(config.get("xmom_shadow_stream_max_age_seconds", 20))
    rows: list[dict[str, Any]] = []
    for symbol, ticker in (snapshot.get("tickers") or {}).items():
        upper = str(symbol).upper()
        if not upper.endswith("USDT") or upper in {"BTCUSDT", "ETHUSDT"}:
            continue
        if not _fresh(ticker, now, max_age):
            continue
        try:
            price = float(ticker.get("lastPrice") or 0)
            momentum = float(ticker.get("priceChangePercent") or 0)
            quote_volume = float(ticker.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0 or quote_volume < min_volume:
            continue
        rows.append(
            {
                "symbol": upper,
                "last_price": price,
                "momentum_24h_pct": momentum,
                "quote_volume": quote_volume,
            }
        )
    min_universe = int(config.get("xmom_shadow_min_universe", 60))
    if len(rows) < min_universe:
        return {
            "status": "insufficient_universe",
            "reason": f"实时有效币种不足：{len(rows)}/{min_universe}",
            "universe_size": len(rows),
            "execution_delay_seconds": delay_seconds,
        }
    btc = (snapshot.get("tickers") or {}).get("BTCUSDT") or {}
    if not _fresh(btc, now, max_age):
        return {
            "status": "missing_btc",
            "reason": "BTC 实时行情过期，跳过本小时",
            "universe_size": len(rows),
            "execution_delay_seconds": delay_seconds,
        }
    btc_momentum = float(btc.get("priceChangePercent") or 0)
    ordered = sorted(rows, key=lambda item: item["momentum_24h_pct"])
    middle = len(ordered) // 2
    breadth = (
        ordered[middle]["momentum_24h_pct"]
        if len(ordered) % 2
        else (ordered[middle - 1]["momentum_24h_pct"] + ordered[middle]["momentum_24h_pct"]) / 2
    )
    if btc_momentum > 0 and breadth > 0:
        selected = ordered[-1]
        direction = "LONG"
        regime = "broad_up"
    elif btc_momentum < 0 and breadth < 0:
        selected = ordered[0]
        direction = "SHORT"
        regime = "broad_down"
    else:
        return {
            "status": "mixed_market",
            "reason": "BTC 与山寨币广度不同向，本小时不做趋势滚仓影子",
            "universe_size": len(rows),
            "btc_momentum_24h_pct": round(btc_momentum, 6),
            "breadth_median_24h_pct": round(breadth, 6),
            "execution_delay_seconds": delay_seconds,
        }
    rank = ordered.index(selected)
    percentile = rank / max(1, len(ordered) - 1)
    return {
        "status": "selected",
        "reason": "BTC 与山寨币广度同向，选择横截面动量最强端",
        **selected,
        "direction": direction,
        "market_regime": regime,
        "rank_percentile": round(percentile, 6),
        "universe_size": len(rows),
        "btc_momentum_24h_pct": round(btc_momentum, 6),
        "breadth_median_24h_pct": round(breadth, 6),
        "execution_delay_seconds": delay_seconds,
        "signal_hour": now.replace(minute=0, second=0, microsecond=0).isoformat(),
    }


def _atr(rows: list[list[Any]], period: int = 24) -> float:
    if len(rows) < period + 1:
        return 0.0
    values: list[float] = []
    for index in range(1, len(rows)):
        high = float(rows[index][2])
        low = float(rows[index][3])
        previous_close = float(rows[index - 1][4])
        values.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return sum(values[-period:]) / period if len(values) >= period else 0.0


def build_cross_sectional_shadow_candidate(
    client: BinanceFuturesClient,
    snapshot: dict[str, Any],
    config: dict[str, Any],
    now: datetime,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    selection = select_cross_sectional_signal(snapshot, config, now)
    if selection.get("status") != "selected":
        return None, selection
    exchange_info = client.exchange_info()
    selected_symbol = str(selection["symbol"])
    symbol_info = next(
        (
            item
            for item in exchange_info.get("symbols", [])
            if str(item.get("symbol") or "") == selected_symbol
        ),
        None,
    )
    onboard_ms = int((symbol_info or {}).get("onboardDate") or 0)
    if onboard_ms <= 0:
        return None, {
            **selection,
            "status": "missing_onboard_date",
            "reason": "Binance 未返回该币种上线时间，本小时研究影子跳过",
        }
    symbol_age_days = (now.timestamp() * 1000 - onboard_ms) / 86_400_000
    minimum_age_days = int(config.get("xmom_shadow_min_onboard_age_days", 30))
    if symbol_age_days < minimum_age_days:
        return None, {
            **selection,
            "status": "listing_too_new",
            "reason": (
                f"最强动量币上市仅 {symbol_age_days:.1f} 天，"
                f"低于研究门槛 {minimum_age_days} 天，本小时不递补追逐次强币"
            ),
            "symbol_age_days": round(symbol_age_days, 4),
            "minimum_onboard_age_days": minimum_age_days,
        }
    selection["symbol_age_days"] = round(symbol_age_days, 4)
    selection["minimum_onboard_age_days"] = minimum_age_days
    bars = client.klines(str(selection["symbol"]), "1h", 72)
    current_hour_ms = int(now.replace(minute=0, second=0, microsecond=0).timestamp() * 1000)
    bars = [row for row in bars if row and int(row[0]) < current_hour_ms]
    atr = _atr(bars, int(config.get("xmom_shadow_atr_period", 24)))
    entry = float(selection["last_price"])
    if atr <= 0 or entry <= 0:
        return None, {**selection, "status": "missing_atr", "reason": "1小时 ATR 数据不足"}
    stop_atr = float(config.get("xmom_shadow_stop_atr", 1.2))
    reward_r = float(config.get("xmom_shadow_reward_r", 1.8))
    max_stop_pct = float(config.get("xmom_shadow_max_stop_pct", 15.0))
    stop_distance = min(stop_atr * atr, entry * max_stop_pct / 100)
    sign = 1 if selection["direction"] == "LONG" else -1
    stop = entry - sign * stop_distance
    take = entry + sign * stop_distance * reward_r
    signal_time_ms = int(now.timestamp() * 1000)
    episode_minutes = max(30, int(config.get("xmom_shadow_episode_minutes", 360)))
    episode_bucket = int(now.timestamp() // (episode_minutes * 60))
    episode_id = ":".join(
        (
            STRATEGY_VERSION,
            str(selection["symbol"]),
            str(selection["direction"]),
            str(episode_bucket),
        )
    )
    candidate = {
        "symbol": selection["symbol"],
        "direction": selection["direction"],
        "entry_type": "xmom_24h_extreme",
        "mode": "research_shadow",
        "strategy": "cross_sectional_momentum_shadow",
        "strategy_family": STRATEGY_FAMILY,
        "strategy_version": STRATEGY_VERSION,
        "strategy_role": "challenger",
        "strategy_generation": "xmom-realtime-shadow",
        "evidence_type": "independent_realtime",
        "shadow_force_eligible": True,
        "shadow_single_position": True,
        "shadow_max_hold_minutes": int(config.get("xmom_shadow_max_hold_hours", 12)) * 60,
        # One continuous same-symbol/same-direction move is one observation,
        # even when a shadow closes before the trend itself has ended.
        "shadow_dedupe_key": episode_id,
        "score": 100.0,
        "passed": False,
        "decision_reason": "独立横截面动量研究影子，不参与 V5.2 实盘准入或仓位",
        "market_state": {"state": selection["market_regime"]},
        "ticker": {"last": entry},
        "signal": {
            "signal": selection["direction"],
            "last_price": entry,
            "atr": atr,
            "stop": stop,
            "take_profit": take,
            "protection_profile": {
                "stop_atr": stop_atr,
                "take_profit_atr": stop_atr * reward_r,
                "max_hold_seconds": int(config.get("xmom_shadow_max_hold_hours", 12)) * 3600,
            },
        },
        "signal_time_ms": signal_time_ms,
        "opportunity_id": episode_id,
        "event_id": episode_id,
        "event_group_id": (
            f"{STRATEGY_VERSION}:{selection['symbol']}:"
            f"{selection['direction']}:wave:{episode_bucket}"
        ),
        "parameter_fingerprint": (
            f"{STRATEGY_VERSION}:stop={stop_atr}:r={reward_r}:"
            f"hold={int(config.get('xmom_shadow_max_hold_hours', 12))}:cost="
            f"{float(config.get('shadow_round_trip_cost_pct', 0.12))}"
        ),
        "research_context": {
            **selection,
            "episode_minutes": episode_minutes,
            "episode_bucket": episode_bucket,
        },
    }
    return candidate, {
        **selection,
        "status": "candidate_ready",
        "atr_1h": round(atr, 10),
        "episode_minutes": episode_minutes,
        "episode_id": episode_id,
    }


def _run(config_provider: Callable[[], dict[str, Any]]) -> None:
    evaluated_hour = ""
    _write_status(
        {
            "enabled": True,
            "strategy_family": STRATEGY_FAMILY,
            "strategy_version": STRATEGY_VERSION,
            "status": "waiting_window",
            "reason": "研究线程已启动，等待整点后 60-150 秒评估",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    while True:
        try:
            config = config_provider()
            enabled = bool(config.get("xmom_shadow_enabled", True))
            now = datetime.now(timezone.utc)
            hour = now.replace(minute=0, second=0, microsecond=0).isoformat()
            base_status = {
                "enabled": enabled,
                "strategy_family": STRATEGY_FAMILY,
                "strategy_version": STRATEGY_VERSION,
                "updated_at": now.isoformat(),
            }
            if not enabled:
                _write_status({**base_status, "status": "disabled", "reason": "配置已关闭"})
            elif hour != evaluated_hour:
                snapshot = read_snapshot()
                candidate, status = build_cross_sectional_shadow_candidate(
                    BinanceFuturesClient(
                        str(config.get("api_key") or ""),
                        str(config.get("api_secret") or ""),
                        str(config.get("binance_base_url") or "https://fapi.binance.com"),
                    ),
                    snapshot,
                    config,
                    now,
                )
                if status.get("status") not in {"outside_entry_window"}:
                    if status.get("status") in {"candidate_ready", "mixed_market"}:
                        evaluated_hour = hour
                    result = update_shadow_trades([candidate], config) if candidate else None
                    _write_status({**base_status, **status, "shadow_result": result})
        except Exception as exc:
            _write_status(
                {
                    "enabled": True,
                    "strategy_family": STRATEGY_FAMILY,
                    "strategy_version": STRATEGY_VERSION,
                    "status": "error",
                    "reason": str(exc),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            record_event_throttled(
                "warning",
                "xmom_shadow",
                "横截面动量研究影子评估失败，实盘不受影响。",
                {"error": str(exc)},
                throttle_seconds=300,
            )
        time.sleep(5)


def start_cross_sectional_momentum_thread(config_provider: Callable[[], dict[str, Any]]) -> None:
    global _THREAD
    with _LOCK:
        if _THREAD and _THREAD.is_alive():
            return
        _THREAD = threading.Thread(
            target=_run,
            args=(config_provider,),
            name="xmom-research-shadow",
            daemon=True,
        )
        _THREAD.start()
