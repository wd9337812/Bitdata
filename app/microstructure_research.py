"""Paper-only microstructure research built on the existing Binance WebSocket feed.

This module intentionally cannot return an executable decision.  It turns the
same 100 ms book / aggregate-trade data used by the optional scalp mode into
short-lived shadow opportunities, so the S0 route can measure whether those
features add value before any future live-routing discussion.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.market_stream import stream_depth


STRATEGY_FAMILY = "microstructure_research"
STRATEGY_VERSION = "v5.4-microstructure-shadow"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _notional(levels: list[Any]) -> float:
    total = 0.0
    for level in levels[:5]:
        try:
            total += float(level[0]) * float(level[1])
        except (IndexError, TypeError, ValueError):
            continue
    return total


def _direction(candidate: dict[str, Any]) -> str:
    signal = candidate.get("signal") or {}
    return str(candidate.get("direction") or signal.get("signal") or "LONG").upper()


def _price(candidate: dict[str, Any], depth: dict[str, Any]) -> float:
    signal = candidate.get("signal") or {}
    ticker = candidate.get("ticker") or {}
    value = float(signal.get("last_price") or ticker.get("last") or depth.get("mid_price") or 0.0)
    return value


def evaluate_candidate(candidate: dict[str, Any], config: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Return a paper candidate only after independent book and trade-flow checks."""
    symbol = str(candidate.get("symbol") or "").upper()
    direction = _direction(candidate)
    max_age = int(config.get("microstructure_research_stream_max_age_seconds", 8))
    depth = stream_depth(symbol, max_age_seconds=max_age) if symbol else None
    base = {
        "symbol": symbol,
        "direction": direction,
        "strategy_family": STRATEGY_FAMILY,
        "strategy_version": STRATEGY_VERSION,
        "strategy_role": "research",
        "strategy_generation": "microstructure-websocket-shadow",
        "evidence_type": "microstructure",
    }
    if not depth:
        return None, {**base, "eligible": False, "reason": "盘口或逐笔成交尚未就绪"}
    price = _price(candidate, depth)
    bids = list(depth.get("bids") or [])
    asks = list(depth.get("asks") or [])
    bid_notional = _notional(bids)
    ask_notional = _notional(asks)
    total_book = bid_notional + ask_notional
    book_imbalance = (bid_notional - ask_notional) / total_book if total_book else 0.0
    flow_notional = float(depth.get("trade_flow_notional") or 0.0)
    flow_imbalance = float(depth.get("trade_flow_imbalance") or 0.0)
    microprice_edge_bps = float(depth.get("microprice_edge_bps") or 0.0)
    spread_pct = float(depth.get("spread_pct") or 999.0)
    depth_notional = float(depth.get("depth_notional") or min(bid_notional, ask_notional))
    side = 1.0 if direction == "LONG" else -1.0
    minimum_depth = float(config.get("microstructure_research_min_depth_notional_usdt", 750.0))
    maximum_spread = float(config.get("microstructure_research_max_spread_pct", 0.10))
    minimum_flow = float(config.get("microstructure_research_min_flow_notional_usdt", 15_000.0))
    minimum_imbalance = float(config.get("microstructure_research_min_abs_imbalance", 0.10))
    confirmations = {
        "盘口深度": depth_notional >= minimum_depth,
        "点差": spread_pct <= maximum_spread,
        "盘口方向": side * book_imbalance >= minimum_imbalance,
        "主动成交": flow_notional >= minimum_flow and side * flow_imbalance >= minimum_imbalance,
        "微价格": side * microprice_edge_bps >= 0.0,
    }
    passed_count = sum(confirmations.values())
    minimum_confirmations = int(config.get("microstructure_research_min_confirmations", 4))
    # For a microstructure setup, directional book pressure and aggressive
    # trade flow are its two causal features.  A generic four-out-of-five
    # count must not accidentally admit a candidate when either is opposite.
    directional_core = confirmations["盘口方向"] and confirmations["主动成交"]
    eligible = price > 0 and directional_core and passed_count >= minimum_confirmations
    score = round(
        _clamp(
            45.0
            + min(depth_notional / max(minimum_depth, 1.0), 2.0) * 12.0
            + side * book_imbalance * 20.0
            + min(flow_notional / max(minimum_flow, 1.0), 2.0) * 10.0
            + side * flow_imbalance * 20.0
            + side * microprice_edge_bps * 1.5,
            0.0,
            100.0,
        ),
        3,
    )
    metrics = {
        "score": score,
        "eligible": eligible,
        "confirmation_count": passed_count,
        "confirmations": confirmations,
        "spread_pct": round(spread_pct, 6),
        "depth_notional": round(depth_notional, 3),
        "book_imbalance": round(book_imbalance, 5),
        "flow_notional": round(flow_notional, 3),
        "flow_imbalance": round(flow_imbalance, 5),
        "microprice_edge_bps": round(microprice_edge_bps, 4),
        "reason": "盘口与主动成交同向" if eligible else " / ".join(key for key, value in confirmations.items() if not value),
    }
    if not eligible:
        return None, {**base, **metrics}
    stop_pct = _clamp(float(config.get("microstructure_research_stop_pct", 0.25)), 0.05, 5.0)
    take_profit_pct = _clamp(float(config.get("microstructure_research_take_profit_pct", 0.35)), 0.05, 10.0)
    sign = 1.0 if direction == "LONG" else -1.0
    return {
        **base,
        "mode": "research_shadow",
        "strategy": "microstructure_research",
        "symbol": symbol,
        "direction": direction,
        "passed": False,
        "score": score,
        "entry_type": "盘口主动成交共振",
        "decision_reason": "仅研究影子：" + metrics["reason"],
        "shadow_force_eligible": True,
        "shadow_max_hold_minutes": int(config.get("microstructure_research_max_hold_minutes", 5)),
        "shadow_dedupe_key": f"{STRATEGY_VERSION}:{symbol}:{direction}:{int(datetime.now(timezone.utc).timestamp() // 60)}",
        "signal": {
            "signal": direction,
            "last_price": price,
            "stop": price * (1.0 - sign * stop_pct / 100.0),
            "take_profit": price * (1.0 + sign * take_profit_pct / 100.0),
        },
        "microstructure": metrics,
    }, {**base, **metrics}


def build_research_candidates(candidates: list[dict[str, Any]], config: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not config.get("microstructure_research_enabled", True):
        return [], {"enabled": False, "strategy_version": STRATEGY_VERSION, "reason": "研究通道已关闭"}
    limit = max(1, int(config.get("microstructure_research_candidate_limit", 8)))
    results: list[dict[str, Any]] = []
    inspected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in candidates:
        key = (str(item.get("symbol") or "").upper(), _direction(item))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        candidate, metrics = evaluate_candidate(item, config)
        inspected.append(metrics)
        if candidate:
            results.append(candidate)
        if len(inspected) >= limit:
            break
    best = max(inspected, key=lambda item: float(item.get("score") or 0), default={})
    return results, {
        "enabled": True,
        "strategy_version": STRATEGY_VERSION,
        "inspected": len(inspected),
        "qualified": len(results),
        "best": best,
        "reason": "仅记录影子，不参与实盘准入、仓位或保护单",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
