from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Callable

from app.binance_client import BinanceFuturesClient
from app.binance_rate import BinanceRateLimitError, request_priority
from app.cross_sectional_momentum import _atr, _fresh
from app.market_stream import data_dir, read_snapshot
from app.shadow_trading import ensure_shadow_tables, update_shadow_trades
from app.telemetry import connect
from app.telemetry import record_event_throttled


STRATEGY_FAMILY = "adaptive_30d_momentum"
STRATEGY_VERSION = "s0_xmom_30d_paper_v2"
LIVE_STRATEGY_VERSION = "s0_xmom_30d_live_v1"
HISTORICAL_DIRECTION_SEED = {
    "LONG": [
        -15.600000000000001, -3.0947665597572334, -15.600000000000001,
        -11.779446219382317, 23.621838695943076, -6.719142572283142,
        -8.918382413594005, -6.7039901917075415, 37.60395738203957,
        42.73153347732181, -15.600000000000001, -9.026680244399186,
        20.062913602941165, 35.969642508302414, 21.921341920023973,
        -7.039897061265142, 18.64299217731422, -5.737005810641448,
        39.46159014557671, -5.892835125178552, -9.843697478991608,
        44.40000000000001, 44.39999999999999, -12.86401351823797,
        -8.304490291262134, 17.526417233560075, 41.82775101889589,
        2.0046986721143916, -10.121189601744517, -15.600000000000001,
        -12.884121949070392, -15.600000000000001, -13.623417644923151,
        -15.600000000000001, 44.39999999999999, 44.40000000000001,
        44.39999999999999, 44.39999999999999, 44.39999999999999,
        -15.600000000000001, -15.600000000000001, 4.067688860057795,
        -15.600000000000001, 4.5786345949770055, 44.39999999999999,
        44.39999999999999, -15.600000000000001, 17.384630314475302,
    ],
    "SHORT": [
        17.546214099216705, 27.582200017685032, 10.69233067729083,
        -8.665208065208073, -11.041021269686645, 13.909291840774152,
        -12.434673249922445, -4.928969834945928, 11.093888621022176,
        9.980438756855582, 17.517522432701902, 14.617391304347828,
        -8.321190821408319, 18.42462861610633, 25.649209361163816,
        19.292207278480987, -4.604714258225905, -5.612354394634677,
        -7.917629179331297, -6.463883231500333, -5.43179419525066,
        -6.143059125964, 4.679312461632902, -5.11289398280803,
        -5.350581272458265, -7.730453657477753, 8.160194109321306,
        10.34062708472312, 13.411799410029525, -11.134452296819774,
        14.280366285939339, -3.8776396607195225, -2.7900519673348163,
        -5.417380352644851, -4.816548363174044, -5.285016424711241,
        19.431502533899447, -15.59999999999999, -0.6, 44.39999999999999,
        -8.581631314235727, -14.37388535031847, 6.23836589698047,
        43.18304692663582, -15.59999999999999, -15.59999999999999,
        -15.600000000000014, -15.59999999999999,
    ],
}
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


def current_adaptive_30d_candidate(now: datetime | None = None) -> dict[str, Any] | None:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    status = adaptive_30d_momentum_status()
    candidate = status.get("candidate")
    if (
        status.get("status") != "candidate_ready"
        or str(status.get("strategy_version") or "") != STRATEGY_VERSION
        or not isinstance(candidate, dict)
    ):
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
    stop_atr = float(config.get("adaptive_30d_shadow_stop_atr", 3.0))
    reward_r = float(config.get("adaptive_30d_shadow_reward_r", 3.0))
    max_stop_pct = float(config.get("adaptive_30d_shadow_max_stop_pct", 15.0))
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
    hold_hours = int(config.get("adaptive_30d_shadow_max_hold_hours", 168))
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
        "execution_constraints": _option_constraints(exchange_info, selected["symbol"]),
        "signal": {
            "signal": direction,
            "last_price": entry,
            "atr": metric["atr_24h"],
            "stop": stop,
            "take_profit": take,
            "protection_profile": {
                "protection_version": "adaptive_30d_daily_v1",
                "stop_atr": stop_atr,
                "take_profit_atr": stop_atr * reward_r,
                "max_hold_seconds": hold_hours * 3600,
                "runtime_intraday_trailing_enabled": False,
                "daily_stop_audit_enabled": False,
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


def adaptive_30d_direction_gate(
    direction: str,
    config: dict[str, Any],
    *,
    closed_net_pcts: list[float] | None = None,
) -> dict[str, Any]:
    normalized = str(direction or "").upper()
    window = max(3, int(config.get("adaptive_30d_live_direction_window", 8)))
    if closed_net_pcts is None:
        closed_net_pcts = []
        try:
            with connect() as conn:
                ensure_shadow_tables(conn)
                rows = conn.execute(
                    "SELECT net_pnl, notional FROM shadow_trades "
                    "WHERE status = 'CLOSED' AND strategy_family = ? "
                    "AND strategy_version = ? AND direction = ? "
                    "ORDER BY closed_at ASC, id ASC",
                    (STRATEGY_FAMILY, STRATEGY_VERSION, normalized),
                ).fetchall()
            closed_net_pcts = [
                float(row["net_pnl"] or 0) / max(float(row["notional"] or 0), 1e-9) * 100
                for row in rows
            ]
        except Exception:
            closed_net_pcts = []
    seed = [float(value) for value in HISTORICAL_DIRECTION_SEED.get(normalized, [])]
    paper = [float(value) for value in closed_net_pcts]
    minimum = max(3, int(config.get("adaptive_30d_live_direction_min_trades", 3)))
    threshold = float(config.get("adaptive_30d_live_direction_min_profit_factor", 1.25))
    if len(paper) < window:
        outcomes = seed
    else:
        outcomes = paper[-window:]
    gains = sum(value for value in outcomes if value > 0)
    losses = -sum(value for value in outcomes if value < 0)
    profit_factor = gains / losses if losses > 0 else (999.0 if gains > 0 else 0.0)
    allowed = len(outcomes) >= minimum and profit_factor >= threshold
    return {
        "allowed": allowed,
        "direction": normalized,
        "trades": len(outcomes),
        "profit_factor": round(profit_factor, 6),
        "net_pct_points": round(sum(outcomes), 6),
        "minimum_trades": minimum,
        "minimum_profit_factor": threshold,
        "source": "audited_history_plus_current_version_shadow",
    }


def build_adaptive_30d_live_decision(
    config: dict[str, Any],
    state: dict[str, Any],
    account: dict[str, Any],
    now: datetime | None = None,
    *,
    candidate: dict[str, Any] | None = None,
    closed_net_pcts: list[float] | None = None,
) -> dict[str, Any]:
    from app.market_tsmom_consensus_shadow import _size_execution_option

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    candidate = candidate or current_adaptive_30d_candidate(current)
    base = {
        "strategy_family": STRATEGY_FAMILY,
        "strategy_version": LIVE_STRATEGY_VERSION,
        "mode": "adaptive_30d_momentum",
        "strategy": "adaptive_30d_momentum_live",
    }
    if not candidate:
        return {
            **base,
            "action": "WAIT",
            "reason": "adaptive_30d_no_active_signal",
            "risk": {"allowed": False, "reason": "adaptive_30d_no_active_signal"},
        }
    positions = [
        item for item in account.get("positions", []) or []
        if abs(float(item.get("positionAmt") or item.get("amount") or 0)) > 0
    ]
    if positions:
        return {
            **base,
            "action": "WAIT",
            "reason": "adaptive_30d_position_already_open",
            "risk": {"allowed": False, "reason": "max_open_positions"},
            "candidate": candidate,
        }
    if not config.get("adaptive_30d_live_new_entries_enabled", True):
        return {
            **base,
            "action": "WAIT",
            "reason": "adaptive_30d_new_entries_disabled",
            "risk": {"allowed": False, "reason": "adaptive_30d_new_entries_disabled"},
            "candidate": candidate,
        }
    boundary_value = candidate.get("signal_boundary") or (
        candidate.get("research_context") or {}
    ).get("signal_boundary")
    try:
        boundary = datetime.fromisoformat(str(boundary_value or "").replace("Z", "+00:00"))
        if boundary.tzinfo is None:
            boundary = boundary.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        boundary = None
    entry_window_hours = max(
        1, min(6, int(config.get("adaptive_30d_live_entry_window_hours", 2)))
    )
    if boundary is None or current > boundary + timedelta(hours=entry_window_hours):
        return {
            **base,
            "action": "WAIT",
            "reason": "adaptive_30d_entry_window_expired",
            "risk": {"allowed": False, "reason": "adaptive_30d_entry_window_expired"},
            "candidate": candidate,
        }
    if str(state.get("adaptive_30d_live_entry_day") or "") == current.date().isoformat():
        return {
            **base,
            "action": "WAIT",
            "reason": "adaptive_30d_signal_already_traded_today",
            "risk": {"allowed": False, "reason": "adaptive_30d_signal_already_traded_today"},
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
    direction = str(candidate.get("direction") or "").upper()
    gate = adaptive_30d_direction_gate(
        direction, config, closed_net_pcts=closed_net_pcts
    )
    if not gate["allowed"]:
        return {
            **base,
            "action": "WAIT",
            "reason": "adaptive_30d_direction_gate_blocked",
            "risk": {"allowed": False, "reason": "adaptive_30d_direction_gate_blocked"},
            "candidate": {**candidate, "direction_gate": gate},
        }
    signal = dict(candidate.get("signal") or {})
    entry = float(signal.get("last_price") or candidate.get("ticker", {}).get("last") or 0)
    atr = float(signal.get("atr") or 0)
    live_stop_atr = float(config.get("adaptive_30d_live_stop_atr", 2.5))
    live_reward_r = float(config.get("adaptive_30d_live_reward_r", 3.5))
    live_max_stop_pct = float(config.get("adaptive_30d_live_max_stop_pct", 12.0))
    if entry > 0 and atr > 0:
        sign = 1.0 if direction == "LONG" else -1.0
        stop_distance = min(
            live_stop_atr * atr,
            entry * live_max_stop_pct / 100.0,
        )
        stop = entry - sign * stop_distance
        take = entry + sign * stop_distance * live_reward_r
        hold_hours = int(config.get("adaptive_30d_shadow_max_hold_hours", 120))
        signal["stop"] = stop
        signal["take_profit"] = take
        signal["protection_profile"] = {
            "protection_version": "adaptive_30d_daily_v1",
            "stop_atr": live_stop_atr,
            "take_profit_atr": live_stop_atr * live_reward_r,
            "max_hold_seconds": hold_hours * 3600,
            "runtime_intraday_trailing_enabled": False,
            "daily_stop_audit_enabled": False,
        }
        candidate = {**candidate, "signal": signal}
    equity = float(account.get("equity") or 0)
    available = max(0.0, float(account.get("available_balance") or equity))
    hard_stop = float(config.get("hard_stop_equity", 5.0))
    reserve = float(config.get("adaptive_30d_live_hard_stop_reserve_usdt", 0.50))
    minimum_equity = float(config.get("adaptive_30d_live_min_equity_usdt", 10.0))
    if equity < minimum_equity or equity <= hard_stop + reserve:
        return {
            **base,
            "action": "WAIT",
            "reason": "adaptive_30d_insufficient_hard_stop_headroom",
            "risk": {"allowed": False, "reason": "adaptive_30d_insufficient_hard_stop_headroom"},
            "candidate": candidate,
            "equity": equity,
        }
    requested_risk_pct = min(
        20.0, max(0.01, float(config.get("adaptive_30d_live_risk_pct", 20.0)))
    )
    risk_tier_enabled = bool(config.get("adaptive_30d_live_risk_tier_enabled", False))
    if risk_tier_enabled:
        tier_equity = max(
            1.0, float(config.get("adaptive_30d_live_risk_tier_equity", 30.0))
        )
        if equity >= tier_equity:
            requested_risk_pct = min(
                22.0,
                max(
                    requested_risk_pct,
                    float(config.get("adaptive_30d_live_risk_tier2_pct", 22.0)),
                ),
            )
    leverage = max(1, min(5, int(config.get("adaptive_30d_live_leverage", 2))))
    margin_fraction = min(
        0.95,
        max(0.05, float(config.get("adaptive_30d_live_margin_pct", 90.0)) / 100),
    )
    sizing = _size_execution_option(
        candidate,
        equity=equity,
        available=available,
        requested_risk_pct=requested_risk_pct,
        leverage=leverage,
        margin_fraction=margin_fraction,
        hard_stop=hard_stop,
        reserve=reserve,
        effective_min_notional=float(config.get("effective_min_order_notional_usdt", 10.0)),
    )
    if not sizing.get("eligible"):
        return {
            **base,
            "action": "WAIT",
            "reason": "adaptive_30d_no_contract_safe_execution",
            "risk": {
                "allowed": False,
                "reason": "adaptive_30d_no_contract_safe_execution",
                "attempt": sizing,
            },
            "candidate": candidate,
            "equity": equity,
        }
    live_candidate = {
        **candidate,
        "mode": "adaptive_30d_momentum",
        "strategy": "adaptive_30d_momentum_live",
        "strategy_version": LIVE_STRATEGY_VERSION,
        "strategy_role": "active",
        "passed": True,
        "direction_gate": gate,
        "risk_pct": float(sizing["actual_risk_pct"]),
        "base_risk_pct": requested_risk_pct,
        "leverage": leverage,
        "margin_pct": margin_fraction * 100,
        "decision_reason": "跨年度验证的30日山寨动量、市场同向与滚动方向门通过",
    }
    live_candidate["signal"] = signal
    return {
        **base,
        "symbol": str(candidate.get("symbol") or "").upper(),
        "action": f"OPEN_{direction}",
        "direction": direction,
        "signal": signal,
        "risk": {
            "allowed": True,
            "reason": "adaptive_30d_live_primary",
            "max_notional": sizing["max_notional"],
            "max_margin": sizing["max_margin"],
            "required_notional": sizing["required_notional"],
        },
        "quantity": float(sizing["quantity"]),
        "estimated_notional": float(sizing["notional"]),
        "risk_pct": float(sizing["actual_risk_pct"]),
        "leverage": leverage,
        "entry_type": "adaptive_xmom_30d_live",
        "decision_reason": live_candidate["decision_reason"],
        "candidate": live_candidate,
        "protection_plan": {
            "enabled": False,
            "initial_stop": float(signal.get("stop") or 0),
            "initial_take_profit": float(signal.get("take_profit") or 0),
            "max_hold_bars": 0,
        },
        "equity": equity,
    }


def _run(config_provider: Callable[[], dict[str, Any]]) -> None:
    evaluated_day = ""
    reported_waiting_day = ""
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
                "live_effect": (
                    "s0_primary"
                    if config.get("adaptive_30d_live_enabled", False)
                    else "none"
                ),
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
                if status.get("status") == "outside_daily_window":
                    if reported_waiting_day != day:
                        _write_status({**base, **status})
                        reported_waiting_day = day
                else:
                    evaluated_day = day
                    reported_waiting_day = day
                    result = update_shadow_trades([candidate], config) if candidate else None
                    _write_status(
                        {
                            **base,
                            **status,
                            "candidate": candidate,
                            "shadow_result": result,
                            "live_effect": (
                                "s0_primary"
                                if config.get("adaptive_30d_live_enabled", False)
                                else "none"
                            ),
                        }
                    )
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
