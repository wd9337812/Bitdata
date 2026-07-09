from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.market_stream import stream_kline


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _age_seconds(value: Any) -> float:
    parsed = _parse_dt(value)
    if parsed is None:
        return 999999.0
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def _row_move_pct(row: list[Any] | None) -> float:
    if not row or len(row) < 5:
        return 0.0
    open_price = _float(row[1])
    close = _float(row[4])
    return (close - open_price) / close * 100 if close else 0.0


def _row_quote_volume(row: list[Any] | None) -> float:
    if not row or len(row) < 8:
        return 0.0
    return _float(row[7])


def _depth_sides(depth: dict[str, Any]) -> tuple[float, float]:
    bids = depth.get("bids") or []
    asks = depth.get("asks") or []
    bid_notional = 0.0
    ask_notional = 0.0
    for price, qty in bids[:5]:
        bid_notional += _float(price) * _float(qty)
    for price, qty in asks[:5]:
        ask_notional += _float(price) * _float(qty)
    return bid_notional, ask_notional


def _signal_direction_from_event(event: dict[str, Any] | None, one_minute_move_pct: float, ticker_change_pct: float) -> str:
    event = event or {}
    hint = str(event.get("direction_hint") or "").upper()
    if hint in {"LONG", "SHORT"}:
        return hint
    if one_minute_move_pct > 0:
        return "LONG"
    if one_minute_move_pct < 0:
        return "SHORT"
    return "LONG" if ticker_change_pct >= 0 else "SHORT"


def build_scalp_signal(
    *,
    symbol: str,
    direction: str,
    bars: list[list[Any]],
    ticker: dict[str, Any],
    depth: dict[str, Any],
    event: dict[str, Any] | None,
    base_signal: dict[str, Any],
    recent: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    if not config.get("yolo_scalp_orderbook_engine_enabled", True):
        return {"enabled": False, "passed": False, "reason": "engine_disabled", "label": "盘口剥头皮未启用"}

    price = _float(base_signal.get("last_price") or ticker.get("lastPrice"))
    atr_value = _float(base_signal.get("atr"))
    if price <= 0:
        return {"enabled": True, "passed": False, "reason": "price_missing", "label": "缺少价格"}
    if atr_value <= 0 and len(bars) >= 2:
        highs = [_float(row[2]) for row in bars[-14:]]
        lows = [_float(row[3]) for row in bars[-14:]]
        atr_value = (max(highs) - min(lows)) / max(1, min(len(highs), 14))

    one_minute = stream_kline(symbol, "1m", max_age_seconds=int(config.get("yolo_scalp_stream_max_age_seconds", 12)))
    one_minute_row = one_minute.get("row") if isinstance(one_minute, dict) else None
    one_minute_move_pct = _row_move_pct(one_minute_row)
    one_minute_quote_volume = _row_quote_volume(one_minute_row)
    ticker_change_pct = _float(ticker.get("priceChangePercent"))
    event = event or {}
    event_age = _age_seconds(event.get("updated_at"))
    event_move_pct = _float(event.get("move_pct") or abs(_float(event.get("signed_move_pct"))))
    event_quote_volume = _float(event.get("quote_volume"))
    spread_pct = _float(depth.get("spread_pct"), 999.0)
    depth_notional = _float(depth.get("depth_notional"))
    bid_notional, ask_notional = _depth_sides(depth)
    side_total = bid_notional + ask_notional
    imbalance = (bid_notional - ask_notional) / side_total if side_total > 0 else 0.0
    directed_imbalance = imbalance if direction == "LONG" else -imbalance
    intended_direction = _signal_direction_from_event(event, one_minute_move_pct, ticker_change_pct)

    min_spread = float(config.get("yolo_scalp_orderbook_max_spread_pct", 0.08))
    min_depth = float(config.get("yolo_scalp_orderbook_min_depth_notional_usdt", 1000.0))
    min_imbalance = float(config.get("yolo_scalp_orderbook_min_imbalance", 0.08))
    strong_imbalance = float(config.get("yolo_scalp_orderbook_strong_imbalance", 0.18))
    max_event_age = float(config.get("yolo_scalp_orderbook_event_max_age_seconds", 45))
    min_move = float(config.get("yolo_scalp_orderbook_min_1m_move_pct", 0.08))
    min_quote_volume = float(config.get("yolo_scalp_orderbook_min_1m_quote_volume_usdt", 25000.0))
    min_cost_ratio = float(config.get("yolo_scalp_orderbook_min_profit_cost_ratio", 1.15))
    min_net_pct = float(config.get("yolo_scalp_orderbook_min_net_profit_pct", 0.04))

    reasons: list[str] = []
    blockers: list[str] = []
    if spread_pct <= min_spread:
        reasons.append("点差合格")
    else:
        blockers.append(f"点差过大 {spread_pct:.3f}%>{min_spread:.3f}%")
    if depth_notional >= min_depth:
        reasons.append("盘口深度合格")
    else:
        blockers.append(f"盘口深度不足 {depth_notional:.0f}U<{min_depth:.0f}U")
    if directed_imbalance >= min_imbalance:
        reasons.append("盘口同向失衡")
    else:
        blockers.append(f"盘口失衡不足 {directed_imbalance:.3f}<{min_imbalance:.3f}")
    if intended_direction == direction:
        reasons.append("实时方向一致")
    else:
        blockers.append("实时方向不一致")

    event_active = event_age <= max_event_age and (event_move_pct >= min_move or event_quote_volume >= min_quote_volume)
    one_minute_active = abs(one_minute_move_pct) >= min_move or one_minute_quote_volume >= min_quote_volume
    if event_active or one_minute_active:
        reasons.append("实时异动有效")
    else:
        blockers.append("缺少实时异动")

    atr_pct = atr_value / price * 100 if price and atr_value else 0.0
    raw_profit_pct = max(
        abs(one_minute_move_pct) * float(config.get("yolo_scalp_orderbook_move_profit_capture", 0.45)),
        atr_pct * float(config.get("yolo_scalp_orderbook_atr_profit_capture", 0.16)),
        _float(base_signal.get("expected_profit_pct")),
    )
    estimated_cost_pct = float(config.get("estimated_slippage_pct", 0.04)) + float(config.get("taker_fee_pct_round_trip", 0.08))
    cost_ratio = raw_profit_pct / estimated_cost_pct if estimated_cost_pct > 0 else 999.0
    net_profit_pct = raw_profit_pct - estimated_cost_pct
    if cost_ratio >= min_cost_ratio and net_profit_pct >= min_net_pct:
        reasons.append("扣费后有空间")
    else:
        blockers.append(f"扣费后空间不足 成本比{cost_ratio:.2f}/净{net_profit_pct:.3f}%")

    score = 45.0
    score += max(0.0, min((min_spread - spread_pct) / max(min_spread, 0.0001) * 14, 14))
    score += min(depth_notional / max(min_depth, 1) * 8, 18)
    score += min(max(directed_imbalance, 0.0) * 80, 24)
    score += min(abs(one_minute_move_pct) * 20, 18)
    score += min(event_move_pct * 10, 12)
    score += min(cost_ratio * 5, 16)
    score += min(float(recent.get("profit_factor") or 0) * 1.5, 6)
    score = round(max(0.0, min(score, 160.0)), 4)

    if directed_imbalance >= strong_imbalance and event_active:
        entry_type = "orderbook_impact"
        label = "盘口冲击"
    elif one_minute_active and one_minute_quote_volume >= min_quote_volume:
        entry_type = "volume_scalp"
        label = "放量剥头皮"
    elif directed_imbalance >= min_imbalance:
        entry_type = "imbalance_probe"
        label = "失衡试探"
    else:
        entry_type = "scalp_watch"
        label = "剥头皮观察"

    passed = not blockers
    stop_pct = float(config.get("yolo_scalp_orderbook_stop_pct", 0.20))
    take_pct = max(
        float(config.get("yolo_scalp_orderbook_min_take_profit_pct", 0.14)),
        estimated_cost_pct + float(config.get("yolo_scalp_orderbook_target_net_profit_pct", 0.06)),
    )
    if entry_type == "imbalance_probe":
        stop_pct *= 0.8
        take_pct *= 0.9
    is_short = direction == "SHORT"
    stop = price * (1 + stop_pct / 100) if is_short else price * (1 - stop_pct / 100)
    take_profit = price * (1 - take_pct / 100) if is_short else price * (1 + take_pct / 100)
    max_hold_seconds = int(config.get("yolo_scalp_orderbook_max_hold_seconds", 120))
    max_hold_bars = max(1, round(max_hold_seconds / 60))

    return {
        "enabled": True,
        "passed": passed,
        "reason": "passed" if passed else "filters_not_passed",
        "label": label,
        "entry_type": entry_type,
        "score": score,
        "direction": direction,
        "blockers": blockers,
        "reasons": reasons,
        "spread_pct": round(spread_pct, 6),
        "depth_notional": round(depth_notional, 6),
        "bid_notional": round(bid_notional, 6),
        "ask_notional": round(ask_notional, 6),
        "imbalance": round(imbalance, 6),
        "directed_imbalance": round(directed_imbalance, 6),
        "one_minute_move_pct": round(one_minute_move_pct, 6),
        "one_minute_quote_volume": round(one_minute_quote_volume, 6),
        "event_age_seconds": round(event_age, 3),
        "event_move_pct": round(event_move_pct, 6),
        "event_quote_volume": round(event_quote_volume, 6),
        "expected_profit_pct": round(raw_profit_pct, 6),
        "estimated_cost_pct": round(estimated_cost_pct, 6),
        "cost_ratio": round(cost_ratio, 6),
        "net_profit_pct": round(net_profit_pct, 6),
        "signal": {
            "symbol": symbol,
            "signal": direction,
            "strategy": "orderbook_scalp",
            "reason": entry_type,
            "last_price": price,
            "atr": atr_value if atr_value > 0 else price * stop_pct / 100,
            "stop": stop,
            "take_profit": take_profit,
            "expected_profit_pct": raw_profit_pct,
            "trend": True,
            "volatility_ok": True,
            "distance_to_trigger_pct": 0.0,
            "candle_move_pct": abs(one_minute_move_pct),
            "entry_type": entry_type,
            "entry_type_label": label,
            "protection_profile": {
                "stop_pct": stop_pct,
                "take_profit_pct": take_pct,
                "max_hold_bars": max_hold_bars,
                "max_hold_seconds": max_hold_seconds,
                "orderbook_exit": True,
            },
        },
    }
