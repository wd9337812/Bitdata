from __future__ import annotations

import json
import math
import os
import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Callable

from app.binance_client import BinanceFuturesClient
from app.binance_rate import BinanceRateLimitError, request_priority
from app.cross_sectional_momentum import _fresh
from app.exchange_filters import round_step
from app.market_stream import data_dir, read_snapshot
from app.shadow_trading import manage_shadow_strategy_positions, update_shadow_trades
from app.telemetry import record_event_throttled


STRATEGY_FAMILY = "market_tsmom_consensus"
STRATEGY_VERSION = "s0_market_tsmom_bnb_28_56_time5_v4"
FREQUENCY_CHALLENGER_VERSION = "s0_market_tsmom_bnb_28_56_time3_stop15_shadow_v1"
LEGACY_STRATEGY_VERSIONS = frozenset({"s0_market_tsmom_28_56_trailing_v3"})
LIVE_SYMBOL = "BNBUSDT"
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


def status_has_current_day_evaluation(
    status: dict[str, Any],
    now: datetime | None = None,
) -> bool:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if str(status.get("strategy_version") or "") != STRATEGY_VERSION:
        return False
    if status.get("status") not in {"candidate_ready", "no_signal"}:
        return False
    try:
        boundary = datetime.fromisoformat(
            str(status.get("signal_boundary") or "").replace("Z", "+00:00")
        )
        if boundary.tzinfo is None:
            boundary = boundary.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    return boundary.date() == current.date()


def current_market_tsmom_candidate(
    now: datetime | None = None,
) -> dict[str, Any] | None:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    status = market_tsmom_consensus_status()
    candidate = status.get("candidate")
    if status.get("status") != "candidate_ready" or not isinstance(candidate, dict):
        return None
    try:
        boundary = datetime.fromisoformat(
            str(status.get("signal_boundary") or "").replace("Z", "+00:00")
        )
        if boundary.tzinfo is None:
            boundary = boundary.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return candidate if boundary.date() == current.date() else None


def _option_constraints(exchange_info: dict[str, Any], symbol: str) -> dict[str, float | str]:
    info = next(
        (
            item
            for item in exchange_info.get("symbols", [])
            if str(item.get("symbol") or "").upper() == symbol.upper()
        ),
        {},
    )
    filters = {
        str(item.get("filterType") or ""): item
        for item in info.get("filters", [])
    }
    lot = filters.get("LOT_SIZE") or {}
    notional = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL") or {}
    return {
        "step_size": str(lot.get("stepSize") or "0.001"),
        "min_quantity": float(lot.get("minQty") or 0.001),
        "min_notional": float(
            notional.get("notional", notional.get("minNotional", 0)) or 0
        ),
    }


def _size_execution_option(
    option: dict[str, Any],
    *,
    equity: float,
    available: float,
    requested_risk_pct: float,
    leverage: int,
    margin_fraction: float,
    hard_stop: float,
    reserve: float,
    effective_min_notional: float,
) -> dict[str, Any]:
    signal = dict(option.get("signal") or {})
    entry = float(signal.get("last_price") or 0)
    stop = float(signal.get("stop") or 0)
    if entry <= 0 or stop <= 0 or stop >= entry:
        return {"eligible": False, "reason": "invalid_protection"}
    risk_budget = min(
        equity * requested_risk_pct / 100,
        max(0.0, equity - hard_stop - reserve),
    )
    stop_distance = entry - stop
    max_notional = available * margin_fraction * leverage
    raw_quantity = min(risk_budget / stop_distance, max_notional / entry)
    constraints = dict(option.get("execution_constraints") or {})
    step = str(constraints.get("step_size") or "0.001")
    quantity = round_step(raw_quantity, step)
    min_quantity = float(constraints.get("min_quantity") or 0)
    required_notional = max(
        effective_min_notional,
        float(constraints.get("min_notional") or 0),
        min_quantity * entry,
    )
    notional = quantity * entry
    actual_risk_pct = quantity * stop_distance / equity * 100 if equity > 0 else 0.0
    eligible = bool(
        quantity > 0
        and quantity >= min_quantity
        and notional >= required_notional
        and actual_risk_pct <= requested_risk_pct + 1e-9
    )
    return {
        "eligible": eligible,
        "reason": "eligible" if eligible else "below_exchange_minimum",
        "quantity": quantity,
        "raw_quantity": raw_quantity,
        "notional": notional,
        "actual_risk_pct": actual_risk_pct,
        "required_notional": required_notional,
        "max_notional": max_notional,
        "max_margin": available * margin_fraction,
    }


def build_market_tsmom_live_decision(
    config: dict[str, Any],
    state: dict[str, Any],
    account: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    candidate = current_market_tsmom_candidate(current)
    base = {
        "strategy_family": STRATEGY_FAMILY,
        "strategy_version": STRATEGY_VERSION,
        "mode": "market_tsmom",
        "strategy": "market_tsmom_consensus",
    }
    if not candidate:
        return {
            **base,
            "action": "WAIT",
            "reason": "market_tsmom_no_active_signal",
            "risk": {"allowed": False, "reason": "market_tsmom_no_active_signal"},
        }
    positions = [
        item
        for item in account.get("positions", []) or []
        if abs(float(item.get("positionAmt") or item.get("amount") or 0)) > 0
    ]
    if positions:
        return {
            **base,
            "action": "WAIT",
            "reason": "market_tsmom_position_already_open",
            "risk": {"allowed": False, "reason": "max_open_positions"},
            "candidate": candidate,
        }
    if not config.get("market_tsmom_live_new_entries_enabled", True):
        return {
            **base,
            "action": "WAIT",
            "reason": "market_tsmom_new_entries_disabled",
            "risk": {
                "allowed": False,
                "reason": "market_tsmom_new_entries_disabled",
            },
            "candidate": candidate,
        }
    entry_boundary_value = candidate.get("signal_boundary") or (
        candidate.get("research_context") or {}
    ).get("signal_boundary")
    try:
        entry_boundary = datetime.fromisoformat(
            str(entry_boundary_value or "").replace("Z", "+00:00")
        )
        if entry_boundary.tzinfo is None:
            entry_boundary = entry_boundary.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        entry_boundary = None
    entry_window_hours = max(
        1,
        min(6, int(config.get("market_tsmom_bnb_entry_window_hours", 2))),
    )
    if entry_boundary is None or current > entry_boundary + timedelta(hours=entry_window_hours):
        return {
            **base,
            "action": "WAIT",
            "reason": "market_tsmom_entry_window_expired",
            "risk": {
                "allowed": False,
                "reason": "market_tsmom_entry_window_expired",
            },
            "candidate": candidate,
        }
    if state.get("s0_daily_profit_lock_active"):
        return {
            **base,
            "action": "WAIT",
            "reason": "s0_daily_profit_lock",
            "risk": {"allowed": False, "reason": "s0_daily_profit_lock"},
            "candidate": candidate,
        }
    if str(state.get("market_tsmom_live_entry_day") or "") == current.date().isoformat():
        return {
            **base,
            "action": "WAIT",
            "reason": "market_tsmom_signal_already_traded_today",
            "risk": {
                "allowed": False,
                "reason": "market_tsmom_signal_already_traded_today",
            },
            "candidate": candidate,
        }
    equity = float(account.get("equity") or 0)
    available = max(0.0, float(account.get("available_balance") or equity))
    hard_stop = float(config.get("hard_stop_equity", 5.0))
    reserve = float(config.get("market_tsmom_live_hard_stop_reserve_usdt", 0.50))
    minimum_equity = float(config.get("market_tsmom_live_min_equity_usdt", 10.0))
    if equity < minimum_equity or equity <= hard_stop + reserve:
        return {
            **base,
            "action": "WAIT",
            "reason": "market_tsmom_insufficient_hard_stop_headroom",
            "risk": {
                "allowed": False,
                "reason": "market_tsmom_insufficient_hard_stop_headroom",
            },
            "candidate": candidate,
            "equity": equity,
        }
    requested_risk_pct = min(
        30.0,
        max(0.01, float(config.get("market_tsmom_live_risk_pct", 10.0))),
    )
    leverage = max(1, min(3, int(config.get("market_tsmom_live_leverage", 1))))
    margin_fraction = min(
        0.95,
        max(0.05, float(config.get("market_tsmom_live_margin_pct", 90.0)) / 100),
    )
    minimum_notional = float(config.get("effective_min_order_notional_usdt", 10.0))
    options = dict(candidate.get("execution_options") or {})
    if not options:
        options = {str(candidate.get("symbol") or "BTCUSDT"): candidate}
    preferred = [str(candidate.get("live_symbol") or LIVE_SYMBOL).upper()]
    selected_option = None
    sizing = None
    sizing_attempts = {}
    for symbol in preferred:
        option = options.get(symbol)
        if not isinstance(option, dict):
            continue
        attempt = _size_execution_option(
            option,
            equity=equity,
            available=available,
            requested_risk_pct=requested_risk_pct,
            leverage=leverage,
            margin_fraction=margin_fraction,
            hard_stop=hard_stop,
            reserve=reserve,
            effective_min_notional=minimum_notional,
        )
        sizing_attempts[symbol] = attempt
        if attempt.get("eligible"):
            selected_option = option
            sizing = attempt
            break
    if selected_option is None or sizing is None:
        return {
            **base,
            "action": "WAIT",
            "reason": "market_tsmom_no_contract_safe_execution",
            "risk": {
                "allowed": False,
                "reason": "market_tsmom_no_contract_safe_execution",
                "attempts": sizing_attempts,
            },
            "candidate": candidate,
            "equity": equity,
        }
    symbol = str(selected_option.get("symbol") or "").upper()
    signal = dict(selected_option.get("signal") or {})
    entry = float(signal.get("last_price") or 0)
    stop = float(signal.get("stop") or 0)
    quantity = float(sizing["quantity"])
    notional = float(sizing["notional"])
    actual_risk_pct = float(sizing["actual_risk_pct"])
    live_candidate = {
        **candidate,
        **selected_option,
        "mode": "market_tsmom",
        "strategy": "market_tsmom_consensus",
        "strategy_role": "active",
        "passed": True,
        "risk_pct": actual_risk_pct,
        "base_risk_pct": requested_risk_pct,
        "leverage": leverage,
        "margin_pct": margin_fraction * 100,
        "decision_reason": (
            "28/56-day market trend consensus with the preregistered BNB "
            "five-day holding rule and a fixed 10% exchange stop"
        ),
    }
    signal["take_profit"] = entry * 2.0
    signal["protection_profile"] = {
        **dict(signal.get("protection_profile") or {}),
        "protection_version": "market_tsmom_bnb_time5_v4",
        "max_hold_seconds": int(config.get("market_tsmom_bnb_max_hold_hours", 120)) * 3600,
        "runtime_intraday_trailing_enabled": False,
        "daily_stop_audit_enabled": False,
    }
    live_candidate["signal"] = signal
    return {
        **base,
        "symbol": symbol,
        "action": "OPEN_LONG",
        "direction": "LONG",
        "signal": signal,
        "risk": {
            "allowed": True,
            "reason": "market_tsmom_live_takeover",
            "max_notional": sizing["max_notional"],
            "max_margin": sizing["max_margin"],
            "required_notional": sizing["required_notional"],
            "execution_fallback_used": False,
            "attempts": sizing_attempts,
        },
        "quantity": quantity,
        "estimated_notional": notional,
        "risk_pct": actual_risk_pct,
        "leverage": leverage,
        "entry_type": "market_tsmom_bnb_28_56_time5",
        "decision_reason": live_candidate["decision_reason"],
        "candidate": live_candidate,
        "protection_plan": {
            "enabled": False,
            "initial_stop": stop,
            "initial_take_profit": signal["take_profit"],
            "max_hold_bars": 0,
        },
        "equity": equity,
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
    highs = [float(row[2]) for row in scoped]
    lows = [float(row[3]) for row in scoped]
    closes = [float(row[4]) for row in scoped]
    quote_volumes = [float(row[7]) for row in scoped]
    if any(value <= 0 for value in closes):
        return None
    returns = [closes[index] / closes[index - 1] - 1.0 for index in range(1, len(closes))]
    true_ranges = []
    for index in range(1, len(closes)):
        true_ranges.append(
            max(
                highs[index] - lows[index],
                abs(highs[index] - closes[index - 1]),
                abs(lows[index] - closes[index - 1]),
            )
        )
    return {
        "returns": returns,
        "median_quote_volume_30d": median(quote_volumes[-30:]),
        "last_close": closes[-1],
        "atr_10": sum(true_ranges[-10:]) / 10,
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


def _build_execution_option(
    symbol: str,
    snapshot: dict[str, Any],
    daily: dict[str, Any],
    exchange_info: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any] | None:
    ticker = (read_snapshot().get("tickers") or {}).get(symbol) or (
        snapshot.get("tickers") or {}
    ).get(symbol) or {}
    entry = float(ticker.get("lastPrice") or 0)
    if entry <= 0:
        return None
    stop_pct = float(config.get("market_tsmom_shadow_stop_pct", 15.0)) / 100
    atr_multiple = float(config.get("market_tsmom_shadow_atr_multiple", 3.0))
    trailing_stop = float(daily["last_close"]) - atr_multiple * float(daily["atr_10"])
    initial_stop = max(entry * (1.0 - stop_pct), trailing_stop)
    return {
        "symbol": symbol,
        "ticker": {"last": entry},
        "execution_constraints": _option_constraints(exchange_info, symbol),
        "signal": {
            "signal": "LONG",
            "last_price": entry,
            "atr": float(daily["atr_10"]),
            "stop": initial_stop,
            "take_profit": entry * 10.0,
            "protection_profile": {
                "stop_pct": stop_pct * 100,
                "atr_days": 10,
                "atr_multiple": atr_multiple,
                "daily_trailing_stop": trailing_stop,
                "take_profit_mode": "none_time_exit_only",
                "max_hold_seconds": int(
                    config.get("market_tsmom_shadow_max_hold_hours", 480)
                )
                * 3600,
            },
        },
    }


def _build_bnb_time5_execution_option(
    snapshot: dict[str, Any],
    daily: dict[str, Any],
    exchange_info: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any] | None:
    ticker = (read_snapshot().get("tickers") or {}).get(LIVE_SYMBOL) or (
        snapshot.get("tickers") or {}
    ).get(LIVE_SYMBOL) or {}
    entry = float(ticker.get("lastPrice") or 0)
    if entry <= 0:
        return None
    stop_pct = float(config.get("market_tsmom_bnb_stop_pct", 10.0)) / 100
    initial_stop = entry * (1.0 - stop_pct)
    return {
        "symbol": LIVE_SYMBOL,
        "ticker": {"last": entry},
        "execution_constraints": _option_constraints(exchange_info, LIVE_SYMBOL),
        "signal": {
            "signal": "LONG",
            "last_price": entry,
            "atr": float(daily["atr_10"]),
            "stop": initial_stop,
            "take_profit": entry * 2.0,
            "protection_profile": {
                "stop_pct": stop_pct * 100,
                "take_profit_mode": "distant_exchange_safety_time_exit_primary",
                "max_hold_seconds": int(
                    config.get("market_tsmom_bnb_max_hold_hours", 120)
                )
                * 3600,
                "runtime_intraday_trailing_enabled": False,
                "daily_stop_audit_enabled": False,
            },
        },
    }


def build_market_tsmom_shadow_candidate(
    client: BinanceFuturesClient,
    snapshot: dict[str, Any],
    config: dict[str, Any],
    now: datetime,
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
    allow_outside_window: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if not allow_outside_window and not _inside_daily_window(config, now):
        return None, {"status": "outside_daily_window", "reason": "等待每日 UTC 固定评估窗口"}

    exchange_info = client.exchange_info()
    universe = _eligible_symbols(snapshot, exchange_info, config, now)
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
    btc_rows = client.klines("BTCUSDT", "1d", 60)
    btc_daily = completed_daily_series(btc_rows, boundary_ms)
    if not btc_daily:
        return None, {
            **context,
            "status": "missing_btc_daily_history",
            "reason": "BTC daily history is unavailable for ATR trailing protection",
        }
    eth_daily = completed_daily_series(
        client.klines("ETHUSDT", "1d", 60), boundary_ms
    )
    if not eth_daily:
        return None, {
            **context,
            "status": "missing_eth_daily_history",
            "reason": "ETH daily history is unavailable for ATR trailing protection",
        }
    bnb_daily = completed_daily_series(
        client.klines(LIVE_SYMBOL, "1d", 60), boundary_ms
    )
    if not bnb_daily:
        return None, {
            **context,
            "status": "missing_bnb_daily_history",
            "reason": "BNB daily history is unavailable for the fixed five-day rule",
        }
    risk_pct = float(config.get("market_tsmom_shadow_reference_risk_pct", 10.0))
    execution_options = {
        symbol: option
        for symbol, daily in (("BTCUSDT", btc_daily), ("ETHUSDT", eth_daily))
        if (
            option := _build_execution_option(
                symbol, snapshot, daily, exchange_info, config
            )
        )
    }
    bnb_option = _build_bnb_time5_execution_option(
        snapshot, bnb_daily, exchange_info, config
    )
    if bnb_option:
        execution_options[LIVE_SYMBOL] = bnb_option
    if len(execution_options) < 3:
        return None, {
            **context,
            "status": "missing_execution_price",
            "reason": "BNB/BTC/ETH realtime prices are unavailable",
        }
    reference_equity = float(
        config.get("market_tsmom_shadow_execution_equity_usdt", 15.153)
    )
    reference_attempts = {
        symbol: _size_execution_option(
            option,
            equity=reference_equity,
            available=reference_equity,
            requested_risk_pct=risk_pct,
            leverage=max(1, min(3, int(config.get("market_tsmom_live_leverage", 2)))),
            margin_fraction=min(
                0.95,
                max(
                    0.05,
                    float(config.get("market_tsmom_live_margin_pct", 90.0)) / 100,
                ),
            ),
            hard_stop=float(config.get("hard_stop_equity", 5.0)),
            reserve=float(config.get("market_tsmom_live_hard_stop_reserve_usdt", 0.50)),
            effective_min_notional=float(
                config.get("effective_min_order_notional_usdt", 10.0)
            ),
        )
        for symbol, option in execution_options.items()
    }
    shadow_symbol = LIVE_SYMBOL
    shadow_option = execution_options[shadow_symbol]
    day = boundary.date().isoformat()
    candidate = {
        "symbol": shadow_symbol,
        "direction": "LONG",
        "entry_type": "market_tsmom_bnb_28_56_time5",
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
        "shadow_max_hold_minutes": int(
            config.get("market_tsmom_bnb_max_hold_hours", 120)
        )
        * 60,
        "shadow_dedupe_key": f"{STRATEGY_VERSION}:{day}",
        "score": 100.0,
        "passed": False,
        "decision_reason": "28/56 日市场趋势共振独立影子，只验证未来表现，不影响当前实盘",
        "market_state": {"state": "broad_up"},
        "ticker": shadow_option["ticker"],
        "signal": shadow_option["signal"],
        "execution_constraints": shadow_option["execution_constraints"],
        "execution_options": execution_options,
        "live_symbol": LIVE_SYMBOL,
        "signal_boundary": boundary.isoformat(),
        "reference_execution_attempts": reference_attempts,
        "opportunity_id": f"{STRATEGY_VERSION}:{day}",
        "event_id": f"{STRATEGY_VERSION}:{day}",
        "parameter_fingerprint": (
            f"{STRATEGY_VERSION}:fast=28:slow=56:threshold={threshold:.6f}:"
            f"symbol={LIVE_SYMBOL}:stop=0.1000:hold=120:risk={risk_pct:.2f}"
        ),
        "research_context": {
            **context,
            "status": "candidate_ready",
            "symbol": shadow_symbol,
            "preferred_symbol": LIVE_SYMBOL,
            "fallback_symbol": None,
            "direction": "LONG",
            "reference_risk_pct": risk_pct,
            "max_risk_cap_pct": 30.0,
            "gate_policy": "isolated_shadow_no_live_effect",
            "exit_policy": "fixed_10pct_exchange_stop_or_5d_time_exit",
        },
    }
    return candidate, candidate["research_context"]


def build_frequency_challenger_candidate(
    active_candidate: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any] | None:
    if not config.get("market_tsmom_frequency_challenger_enabled", True):
        return None
    candidate = deepcopy(active_candidate)
    signal = dict(candidate.get("signal") or {})
    entry = float(signal.get("last_price") or 0)
    if entry <= 0:
        return None
    stop_pct = float(config.get("market_tsmom_frequency_challenger_stop_pct", 15.0))
    max_hold_hours = int(
        config.get("market_tsmom_frequency_challenger_max_hold_hours", 72)
    )
    signal["stop"] = entry * (1.0 - stop_pct / 100.0)
    signal["protection_profile"] = {
        **dict(signal.get("protection_profile") or {}),
        "stop_pct": stop_pct,
        "take_profit_mode": "distant_exchange_safety_time_exit_primary",
        "max_hold_seconds": max_hold_hours * 3600,
        "runtime_intraday_trailing_enabled": False,
        "daily_stop_audit_enabled": False,
        "protection_version": "market_tsmom_bnb_time3_stop15_shadow_v1",
    }
    day = str(candidate.get("signal_boundary") or "")[:10]
    candidate.update(
        {
            "entry_type": "market_tsmom_bnb_28_56_time3_stop15_shadow",
            "strategy_version": FREQUENCY_CHALLENGER_VERSION,
            "strategy_role": "challenger",
            "strategy_generation": "train-selected-frequency-challenger",
            "shadow_max_hold_minutes": max_hold_hours * 60,
            "shadow_dedupe_key": f"{FREQUENCY_CHALLENGER_VERSION}:{day}",
            "opportunity_id": f"{FREQUENCY_CHALLENGER_VERSION}:{day}",
            "event_id": f"{FREQUENCY_CHALLENGER_VERSION}:{day}",
            "parameter_fingerprint": (
                f"{FREQUENCY_CHALLENGER_VERSION}:fast=28:slow=56:"
                f"symbol={LIVE_SYMBOL}:stop={stop_pct / 100.0:.4f}:"
                f"hold={max_hold_hours}"
            ),
            "decision_reason": (
                "与当前 BNB 28/56 日趋势入场完全相同，只比较三日持有和 15% 固定止损；"
                "仅作独立未来影子，不参与实盘。"
            ),
            "signal": signal,
            "research_context": {
                **dict(candidate.get("research_context") or {}),
                "strategy_version": FREQUENCY_CHALLENGER_VERSION,
                "gate_policy": "isolated_frequency_shadow_no_live_effect",
                "exit_policy": "fixed_15pct_exchange_stop_or_3d_time_exit",
                "selection_note": (
                    "三日版本增加样本频率，但冻结训练期稳健性弱于五日版本，"
                    "因此只收集未来证据。"
                ),
            },
        }
    )
    return candidate


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
                "live_effect": (
                    "s0_takeover"
                    if config.get("market_tsmom_live_enabled", False)
                    else "none"
                ),
            }
            if not enabled:
                _write_status({**base, "status": "disabled", "reason": "配置已关闭"})
            elif day != evaluated_day:
                previous = market_tsmom_consensus_status()
                if status_has_current_day_evaluation(previous, now):
                    active = previous.get("candidate")
                    challenger = previous.get("frequency_challenger")
                    if isinstance(active, dict) and not isinstance(challenger, dict):
                        challenger = build_frequency_challenger_candidate(active, config)
                        if challenger:
                            previous = {
                                **previous,
                                "frequency_challenger": challenger,
                                "frequency_challenger_shadow_result": update_shadow_trades(
                                    [challenger], config
                                ),
                            }
                    evaluated_day = day
                    _write_status({**previous, **base})
                    time.sleep(10)
                    continue
                bootstrap = bool(
                    config.get("market_tsmom_bootstrap_current_day_enabled", True)
                )
                candidate, status = build_market_tsmom_shadow_candidate(
                    BinanceFuturesClient(
                        str(config.get("api_key") or ""),
                        str(config.get("api_secret") or ""),
                        str(config.get("binance_base_url") or "https://fapi.binance.com"),
                    ),
                    read_snapshot(),
                    config,
                    now,
                    allow_outside_window=bootstrap,
                )
                if status.get("status") != "outside_daily_window":
                    evaluated_day = day
                    btc_ticker = (read_snapshot().get("tickers") or {}).get("BTCUSDT") or {}
                    btc_price = float(btc_ticker.get("lastPrice") or 0)
                    management = None
                    if not candidate and status.get("status") == "no_signal" and btc_price > 0:
                        management = {"updated": 0, "closed": 0}
                        prices = (read_snapshot().get("tickers") or {})
                        for symbol in ("BTCUSDT", "ETHUSDT"):
                            price = float((prices.get(symbol) or {}).get("lastPrice") or 0)
                            if price <= 0:
                                continue
                            result = manage_shadow_strategy_positions(
                                STRATEGY_FAMILY,
                                STRATEGY_VERSION,
                                symbol,
                                exit_price=price,
                                outcome="MARKET_SIGNAL_OFF",
                            )
                            management["updated"] += int(result.get("updated") or 0)
                            management["closed"] += int(result.get("closed") or 0)
                    challenger = (
                        build_frequency_challenger_candidate(candidate, config)
                        if candidate
                        else None
                    )
                    shadow_candidates = [
                        item for item in (candidate, challenger) if item is not None
                    ]
                    result = (
                        update_shadow_trades(shadow_candidates, config)
                        if shadow_candidates
                        else None
                    )
                    _write_status(
                        {
                            **base,
                            **status,
                            "candidate": candidate,
                            "frequency_challenger": challenger,
                            "shadow_management": management,
                            "shadow_result": result,
                        }
                    )
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
