from __future__ import annotations

import json
import math
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.market_structure import market_structure, normalize_setup_type
from app.telemetry import connect, db_path


_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _payload_metadata(payload_text: Any) -> tuple[str, str]:
    try:
        payload = json.loads(payload_text or "{}")
    except (json.JSONDecodeError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    decision = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
    candidate = decision.get("candidate") if isinstance(decision.get("candidate"), dict) else {}
    opportunity = candidate.get("opportunity_v4") if isinstance(candidate.get("opportunity_v4"), dict) else {}
    structure = candidate.get("market_structure") if isinstance(candidate.get("market_structure"), dict) else {}
    regime = str(
        payload.get("market_regime")
        or opportunity.get("market_regime")
        or structure.get("market_regime")
        or "unknown"
    ).lower()
    setup = normalize_setup_type(
        payload.get("setup_type")
        or payload.get("signal_type")
        or opportunity.get("setup_type")
        or candidate.get("entry_type")
        or "unknown"
    )
    return regime, setup


def _load_rows(config: dict[str, Any]) -> list[dict[str, Any]]:
    current_version = str(config.get("opportunity_v4_strategy_version") or "v4.8")
    if current_version.lower().startswith("v4.8"):
        seed_version = str(config.get("opportunity_v48_seed_version") or "v4.7")
    else:
        seed_version = str(config.get("opportunity_v47_seed_version") or "v4.6.2")
    versions = tuple(dict.fromkeys((current_version, seed_version)))
    lookback_hours = float(config.get("opportunity_v47_calibration_lookback_hours", 72))
    cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    cutoff_iso = cutoff_dt.isoformat()
    cutoff_ms = int(cutoff_dt.timestamp() * 1000)
    limit = int(config.get("opportunity_v47_calibration_max_rows", 4000))
    placeholders = ",".join("?" for _ in versions)
    rows: list[dict[str, Any]] = []
    with connect() as conn:
        try:
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(shadow_trades)").fetchall()}
            evidence_expr = "COALESCE(evidence_type, 'decision')" if "evidence_type" in columns else "'decision'"
            shadow_rows = conn.execute(
                f"SELECT id, closed_at, symbol, direction, signal_type, market_regime, notional, net_pnl, "
                f"opportunity_id, payload, strategy_version, {evidence_expr} AS evidence_type "
                f"FROM shadow_trades WHERE status = 'CLOSED' AND strategy_family = ? "
                f"AND strategy_version IN ({placeholders}) AND closed_at >= ? ORDER BY id DESC LIMIT ?",
                ("extreme_v4_roll", *versions, cutoff_iso, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            shadow_rows = []
        try:
            live_rows = conn.execute(
                f"SELECT id, close_time, symbol, direction, open_notional, net_pnl, payload, strategy_version "
                f"FROM live_trade_records WHERE strategy_family = ? AND strategy_version IN ({placeholders}) "
                f"AND close_time >= ? ORDER BY id DESC LIMIT ?",
                ("extreme_v4_roll", *versions, cutoff_ms, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            live_rows = []

    now = datetime.now(timezone.utc)
    seen: set[str] = set()
    for row in shadow_rows:
        item = dict(row)
        if str(item.get("evidence_type") or "decision").lower() != "decision":
            continue
        opportunity_id = str(item.get("opportunity_id") or "").strip()
        dedupe_key = f"shadow:{opportunity_id or item.get('id')}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        try:
            closed = datetime.fromisoformat(str(item.get("closed_at") or "").replace("Z", "+00:00"))
            if closed.tzinfo is None:
                closed = closed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        notional = max(float(item.get("notional") or 0), 0.00000001)
        rows.append(
            {
                "source": "shadow",
                "version": str(item.get("strategy_version") or ""),
                "symbol": str(item.get("symbol") or "").upper(),
                "direction": str(item.get("direction") or "").upper(),
                "market_regime": str(item.get("market_regime") or "unknown").lower(),
                "setup_type": normalize_setup_type(item.get("signal_type") or "unknown"),
                "age_hours": max(0.0, (now - closed).total_seconds() / 3600),
                "net_pct": float(item.get("net_pnl") or 0) / notional * 100,
                "opportunity_id": opportunity_id or f"shadow:{item.get('id')}",
            }
        )
    for row in live_rows:
        item = dict(row)
        close_time = int(item.get("close_time") or 0)
        if close_time <= 0:
            continue
        regime, setup = _payload_metadata(item.get("payload"))
        notional = max(float(item.get("open_notional") or 0), 0.00000001)
        rows.append(
            {
                "source": "live",
                "version": str(item.get("strategy_version") or ""),
                "symbol": str(item.get("symbol") or "").upper(),
                "direction": str(item.get("direction") or "").upper(),
                "market_regime": regime,
                "setup_type": setup,
                "age_hours": max(0.0, time.time() - close_time / 1000) / 3600,
                "net_pct": float(item.get("net_pnl") or 0) / notional * 100,
                "opportunity_id": f"live:{item.get('id')}",
            }
        )
    return rows


def calibration_rows(config: dict[str, Any]) -> list[dict[str, Any]]:
    key = f"{db_path()}:{config.get('opportunity_v4_strategy_version', 'v4.8')}"
    now = time.monotonic()
    ttl = float(config.get("opportunity_v47_calibration_cache_seconds", 300))
    cached = _CACHE.get(key)
    if cached and now - cached[0] <= ttl:
        return cached[1]
    value = _load_rows(config)
    _CACHE[key] = (now, value)
    return value


def clear_adaptive_calibration_cache() -> None:
    _CACHE.clear()


def _stats(
    rows: list[dict[str, Any]],
    current_version: str,
    seed_weight: float,
    seed_effective_cap: float,
) -> dict[str, Any]:
    weighted_positive = 0.0
    weighted_negative = 0.0
    weighted_net = 0.0
    total_weight = 0.0
    current_count = 0
    live_count = 0
    symbols: set[str] = set()
    seed_base_weight = sum(
        (2.0 if row.get("source") == "live" else 1.0) * seed_weight
        for row in rows
        if str(row.get("version") or "") != current_version
    )
    seed_scale = min(1.0, seed_effective_cap / seed_base_weight) if seed_base_weight > 0 else 1.0
    seed_effective_weight = 0.0
    for row in rows:
        is_current = str(row.get("version") or "") == current_version
        version_weight = 1.0 if is_current else seed_weight * seed_scale
        source_weight = 2.0 if row.get("source") == "live" else 1.0
        weight = version_weight * source_weight
        value = float(row.get("net_pct") or 0)
        total_weight += weight
        if not is_current:
            seed_effective_weight += weight
        weighted_net += value * weight
        if value > 0:
            weighted_positive += value * weight
        elif value < 0:
            weighted_negative += value * weight
        current_count += int(is_current)
        live_count += int(row.get("source") == "live" and is_current)
        if row.get("symbol"):
            symbols.add(str(row["symbol"]))
    pf = weighted_positive / abs(weighted_negative) if weighted_negative < 0 else (999.0 if weighted_positive > 0 else 0.0)
    return {
        "trades": len(rows),
        "current_trades": current_count,
        "current_live_trades": live_count,
        "effective_samples": round(total_weight, 4),
        "seed_effective_samples": round(seed_effective_weight, 4),
        "symbols": len(symbols),
        "profit_factor": round(pf, 4),
        "net_pct": round(weighted_net, 6),
        "average_net_pct": round(weighted_net / total_weight, 6) if total_weight else 0.0,
    }


def direction_relation(candidate: dict[str, Any]) -> str:
    direction = str(candidate.get("direction") or "").upper()
    regime = str(market_structure(candidate).get("market_regime") or "unknown").lower()
    if regime == "broad_up":
        return "aligned" if direction == "LONG" else "countertrend"
    if regime == "broad_down":
        return "aligned" if direction == "SHORT" else "countertrend"
    structure = market_structure(candidate)
    if structure.get("medium_trend_aligned"):
        return "aligned"
    return "neutral"


def _performance_delta(stats_12h: dict[str, Any], stats_24h: dict[str, Any], config: dict[str, Any]) -> float:
    minimum = int(config.get("opportunity_v47_min_current_samples", 6))
    full = max(minimum, int(config.get("opportunity_v47_full_current_samples", 16)))
    current_samples = max(int(stats_12h["current_trades"]), int(stats_24h["current_trades"]))
    confidence = _clamp(current_samples / full, 0.0, 1.0)

    def score(stats: dict[str, Any]) -> float:
        pf = float(stats.get("profit_factor") or 0)
        avg = float(stats.get("average_net_pct") or 0)
        if pf >= 1.30 and avg > 0:
            return 0.10
        if pf >= 1.05 and avg > 0:
            return 0.05
        if (pf < 0.70 and stats.get("trades")) or avg < -0.05:
            return -0.10
        if (pf < 0.90 and stats.get("trades")) or avg < 0:
            return -0.05
        return 0.0

    raw = score(stats_12h) * 0.60 + score(stats_24h) * 0.40
    if current_samples < minimum:
        confidence = min(confidence, 0.50)
    return raw * confidence


def adaptive_calibration(candidate: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    enabled = bool(config.get("opportunity_v47_adaptive_enabled", True))
    current_version = str(config.get("opportunity_v4_strategy_version") or "v4.8")
    relation = direction_relation(candidate)
    priors = {
        "aligned": float(config.get("opportunity_v47_aligned_multiplier", 1.0)),
        "neutral": float(config.get("opportunity_v47_neutral_multiplier", 0.85)),
        "countertrend": float(config.get("opportunity_v47_countertrend_multiplier", 0.65)),
    }
    if not enabled:
        return {
            "enabled": False,
            "relation": relation,
            "risk_multiplier": 1.0,
            "rank_threshold_delta": 0.0,
            "expectancy_threshold_delta_pct": 0.0,
            "confirmation_delta": 0,
            "reason": "adaptive calibration disabled",
        }

    direction = str(candidate.get("direction") or "").upper()
    regime = str(market_structure(candidate).get("market_regime") or "unknown").lower()
    setup = normalize_setup_type(market_structure(candidate).get("setup_type") or candidate.get("entry_type") or "unknown")
    all_rows = calibration_rows(config)
    cohort = [row for row in all_rows if row["direction"] == direction]
    exact = [row for row in cohort if row["market_regime"] == regime and row["setup_type"] == setup]
    selected = exact if len(exact) >= int(config.get("opportunity_v47_exact_min_samples", 4)) else cohort
    seed_weight = float(config.get("opportunity_v47_seed_weight", 0.25))
    seed_cap = float(config.get("opportunity_v47_seed_effective_sample_cap", 4.0))
    stats_12h = _stats(
        [row for row in selected if row["age_hours"] <= 12], current_version, seed_weight, seed_cap
    )
    stats_24h = _stats(
        [row for row in selected if row["age_hours"] <= 24], current_version, seed_weight, seed_cap
    )
    delta = _performance_delta(stats_12h, stats_24h, config)
    lower = float(config.get("opportunity_v47_min_multiplier", 0.55))
    upper = float(config.get("opportunity_v47_max_multiplier", 1.15))
    max_step = float(config.get("opportunity_v47_max_step", 0.10))
    delta = _clamp(delta, -max_step, max_step)
    multiplier = _clamp(priors[relation] + delta, lower, upper)
    current_samples = max(int(stats_12h["current_trades"]), int(stats_24h["current_trades"]))
    threshold_ready = current_samples >= int(config.get("opportunity_v47_threshold_min_current_samples", 12))
    positive = threshold_ready and delta >= 0.04
    negative = threshold_ready and delta <= -0.04
    rank_delta = -0.02 if positive else 0.02 if negative else 0.0
    expectancy_delta = -0.01 if positive else 0.01 if negative else 0.0
    confirmation_delta = -1 if positive else 1 if negative else 0
    reason = (
        f"{relation}: 12h {stats_12h['current_trades']} current samples, "
        f"24h PF {stats_24h['profit_factor']}, multiplier {multiplier:.2f}x"
    )
    return {
        "enabled": True,
        "schema": "adaptive_v48" if current_version.lower().startswith("v4.8") else "adaptive_v47",
        "relation": relation,
        "direction": direction,
        "market_regime": regime,
        "setup_type": setup,
        "scope": "exact" if selected is exact else "direction",
        "prior_multiplier": round(priors[relation], 4),
        "evidence_delta": round(delta, 4),
        "risk_multiplier": round(multiplier, 4),
        "rank_threshold_delta": rank_delta,
        "expectancy_threshold_delta_pct": expectancy_delta,
        "confirmation_delta": confirmation_delta,
        "threshold_adjustment_ready": threshold_ready,
        "stats_12h": stats_12h,
        "stats_24h": stats_24h,
        "seed_version": (
            str(config.get("opportunity_v48_seed_version") or "v4.7")
            if current_version.lower().startswith("v4.8")
            else str(config.get("opportunity_v47_seed_version") or "v4.6.2")
        ),
        "seed_weight": seed_weight,
        "reason": reason,
    }
