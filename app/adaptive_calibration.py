from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.market_structure import market_structure, normalize_setup_type
from app.telemetry import connect, db_path


_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_GLOBAL_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
GLOBAL_ADAPTIVE_VERSIONS = ("v4.9", "v4.10")


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _payload_metadata(payload_text: Any) -> tuple[str, str, float]:
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
    smart = candidate.get("smart_flow") if isinstance(candidate.get("smart_flow"), dict) else {}
    try:
        smart_score = float(smart.get("score") or 0.0)
    except (TypeError, ValueError):
        smart_score = 0.0
    return regime, setup, max(-1.0, min(1.0, smart_score))


def _load_rows(config: dict[str, Any]) -> list[dict[str, Any]]:
    current_version = str(config.get("opportunity_v4_strategy_version") or "v4.9")
    if current_version.lower().startswith("v4.10"):
        seed_version = str(config.get("opportunity_v410_seed_version") or "v4.9")
    elif current_version.lower().startswith("v4.9"):
        seed_version = ""
    elif current_version.lower().startswith("v4.8"):
        seed_version = str(config.get("opportunity_v48_seed_version") or "v4.7")
    else:
        seed_version = str(config.get("opportunity_v47_seed_version") or "v4.6.2")
    versions = tuple(dict.fromkeys(value for value in (current_version, seed_version) if value))
    lookback_hours = float(
        config.get("opportunity_v49_global_window_hours", 24)
        if current_version.lower().startswith("v4.9")
        else config.get("opportunity_v47_calibration_lookback_hours", 72)
    )
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
        regime, setup, smart_score = _payload_metadata(item.get("payload"))
        notional = max(float(item.get("open_notional") or 0), 0.00000001)
        rows.append(
            {
                "source": "live",
                "version": str(item.get("strategy_version") or ""),
                "symbol": str(item.get("symbol") or "").upper(),
                "direction": str(item.get("direction") or "").upper(),
                "market_regime": regime,
                "setup_type": setup,
                "smart_score": smart_score,
                "age_hours": max(0.0, time.time() - close_time / 1000) / 3600,
                "net_pct": float(item.get("net_pnl") or 0) / notional * 100,
                "opportunity_id": f"live:{item.get('id')}",
            }
        )
    return rows


def calibration_rows(config: dict[str, Any]) -> list[dict[str, Any]]:
    key = f"{db_path()}:{config.get('opportunity_v4_strategy_version', 'v4.9')}"
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
    _GLOBAL_CACHE.clear()


def _global_state_path() -> Path:
    config_path = Path(os.getenv("APP_CONFIG_PATH", "./data/config.json"))
    return config_path.with_name("v49_global_adaptive.json")


def _persist_global_result(result: dict[str, Any]) -> None:
    """Keep a small audit trail without adding high-frequency SQLite writes."""
    path = _global_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        history = list(previous.get("history") or [])[-95:]
        snapshot = {**result, "updated_at": datetime.now(timezone.utc).isoformat()}
        keys = ("version", "positive", "negative", "ready", "rank_threshold_delta", "cost_ratio_delta", "risk_multiplier")
        fingerprint = tuple(snapshot.get(key) for key in keys)
        last_fingerprint = tuple((history[-1] if history else {}).get(key) for key in keys)
        if fingerprint != last_fingerprint:
            history.append(snapshot)
        payload = {"version": result.get("version"), "current": snapshot, "history": history}
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return


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


def _global_stats(rows: list[dict[str, Any]], hours: float) -> dict[str, Any]:
    selected = [row for row in rows if float(row.get("age_hours") or 0) <= hours]
    stats = _stats(selected, str(selected[0].get("version") or "v4.9") if selected else "v4.9", 0.0, 0.0)
    stats["shadow_trades"] = sum(row.get("source") == "shadow" for row in selected)
    stats["live_trades"] = sum(row.get("source") == "live" for row in selected)
    stats["regimes"] = len({str(row.get("market_regime") or "unknown") for row in selected})
    stats["opportunities"] = len({str(row.get("opportunity_id") or "") for row in selected})
    return stats


def _global_v49_calibration(config: dict[str, Any]) -> dict[str, Any]:
    """Calibrate one global V4.9 gate set from current-version evidence only."""
    version = str(config.get("opportunity_v4_strategy_version") or "v4.9")
    key = f"{db_path()}:{version}:global"
    now = time.monotonic()
    cached = _GLOBAL_CACHE.get(key)
    if cached and now - cached[0] <= float(config.get("opportunity_v49_global_update_hours", 2.0)) * 3600:
        return cached[1]
    rows = [row for row in calibration_rows(config) if str(row.get("version") or "") == version]
    stats_12h = _global_stats(rows, 12.0)
    stats_24h = _global_stats(rows, float(config.get("opportunity_v49_global_window_hours", 24.0)))
    stats_72h = _global_stats(rows, 72.0)
    enough = bool(
        stats_24h["shadow_trades"] >= int(config.get("opportunity_v49_global_min_shadow_trades", 20))
        and stats_24h["live_trades"] >= int(config.get("opportunity_v49_global_min_live_trades", 8))
        and stats_24h["symbols"] >= int(config.get("opportunity_v49_global_min_symbols", 3))
        and stats_24h["regimes"] >= int(config.get("opportunity_v49_global_min_regimes", 2))
    )
    pf = float(stats_24h.get("profit_factor") or 0)
    net = float(stats_24h.get("net_pct") or 0)
    positive = enough and pf >= float(config.get("opportunity_v49_global_min_profit_factor", 1.15)) and net > float(config.get("opportunity_v49_global_min_net_pct", 0.0))
    negative = enough and (pf < 1.0 or net < 0)
    rank_delta = 0.0
    expectancy_delta = 0.0
    cost_ratio_delta = 0.0
    confirmation_delta = 0
    risk_multiplier = 1.0
    action = "观察中，样本不足以调整全局门槛"
    target_live_trades = int(config.get("opportunity_v49_global_target_live_trades", 12))
    if positive and stats_24h["live_trades"] < target_live_trades:
        rank_delta = -float(config.get("opportunity_v49_global_positive_relaxation_step", 0.03))
        action = "扣费后正期望但机会偏少，降低全局排名门槛"
    elif positive:
        risk_multiplier = min(
            float(config.get("opportunity_v49_global_max_multiplier", 1.10)),
            1.0 + float(config.get("opportunity_v49_global_positive_relaxation_step", 0.03)),
        )
        action = "当前版本扣费后正期望，允许全局仓位倍率小步恢复"
    elif negative:
        cost_ratio_delta = float(config.get("opportunity_v49_global_negative_tightening_step", 0.05))
        action = "当前版本扣费后负期望，提高全局成本收益门槛"
    context_rows = [row for row in rows if float(row.get("age_hours") or 0) <= 72.0]
    direction_net: dict[str, float] = {"LONG": 0.0, "SHORT": 0.0}
    direction_counts: dict[str, int] = {"LONG": 0, "SHORT": 0}
    regime_net: dict[str, float] = {}
    smart_values: list[float] = []
    for row in context_rows:
        direction = str(row.get("direction") or "").upper()
        value = float(row.get("net_pct") or 0.0)
        if direction in direction_net:
            direction_net[direction] += value
            direction_counts[direction] += 1
        regime = str(row.get("market_regime") or "unknown").lower()
        regime_net[regime] = regime_net.get(regime, 0.0) + value
        smart_values.append(float(row.get("smart_score") or 0.0))
    direction_bias = "NEUTRAL"
    if direction_net["LONG"] - direction_net["SHORT"] > 0.10:
        direction_bias = "LONG"
    elif direction_net["SHORT"] - direction_net["LONG"] > 0.10:
        direction_bias = "SHORT"
    current_regime = max(regime_net, key=regime_net.get) if regime_net else "unknown"
    smart_average = sum(smart_values) / len(smart_values) if smart_values else 0.0

    result = {
        "enabled": bool(config.get("opportunity_v49_global_adaptive_enabled", True)),
        "schema": "adaptive_v49_global",
        "scope": "current_version_global_24h",
        "version": version,
        "ready": enough,
        "positive": positive,
        "negative": negative,
        "stats_12h": stats_12h,
        "stats_24h": stats_24h,
        "stats_72h": stats_72h,
        "market_regime": current_regime,
        "market_regime_state": "trend" if current_regime in {"broad_up", "broad_down", "trend_up", "trend_down"} else "range_or_unknown",
        "global_direction_bias": direction_bias,
        "direction_net_pct": {key: round(value, 6) for key, value in direction_net.items()},
        "direction_counts": direction_counts,
        "smart_flow_global": {
            "available": bool(smart_values),
            "average_score": round(smart_average, 6),
            "bias": "LONG" if smart_average > 0.08 else "SHORT" if smart_average < -0.08 else "NEUTRAL",
            "sample_count": len(smart_values),
            "scope": "current_version_global_72h",
        },
        "risk_multiplier": round(_clamp(risk_multiplier, float(config.get("opportunity_v49_global_min_multiplier", 0.70)), float(config.get("opportunity_v49_global_max_multiplier", 1.10))), 4),
        "rank_threshold_delta": round(rank_delta, 4),
        "expectancy_threshold_delta_pct": round(expectancy_delta, 4),
        "cost_ratio_delta": round(cost_ratio_delta, 4),
        "quality_threshold_delta": 0.0,
        "confirmation_delta": confirmation_delta,
        "adjustment_action": action,
        "adjustment_interval_hours": float(config.get("opportunity_v49_global_update_hours", 2.0)),
        "target_live_trades": target_live_trades,
        "effective_thresholds": {
            "rank_percentile": round(_clamp(float(config.get("opportunity_v44_min_rank_percentile", 0.80)) + rank_delta, float(config.get("opportunity_v49_global_min_rank_percentile", 0.65)), float(config.get("opportunity_v49_global_max_rank_percentile", 0.90))), 4),
            "expected_net_pct": round(_clamp(float(config.get("opportunity_v48_min_expected_net_pct", 0.03)) + expectancy_delta, float(config.get("opportunity_v49_global_min_expectancy_pct", 0.0)), float(config.get("opportunity_v49_global_max_expectancy_pct", 0.20))), 4),
            "cost_ratio": round(_clamp(float(config.get("opportunity_v48_min_cost_ratio", 1.70)) + cost_ratio_delta, float(config.get("opportunity_v49_global_min_cost_ratio", 1.35)), float(config.get("opportunity_v49_global_max_cost_ratio", 2.50))), 4),
            "confirmations": int(_clamp(float(config.get("opportunity_v44_min_confirmations", 3)) + confirmation_delta, float(config.get("opportunity_v49_global_min_confirmations", 3)), float(config.get("opportunity_v49_global_max_confirmations", 5)))),
        },
        "reason": f"全局 V4.9 24h：{stats_24h['trades']} 笔，PF {pf:.2f}，净收益 {net:.4f}%",
    }
    _persist_global_result(result)
    _GLOBAL_CACHE[key] = (now, result)
    return result


def adaptive_calibration(candidate: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    if str(config.get("opportunity_v4_strategy_version") or "").lower().startswith(GLOBAL_ADAPTIVE_VERSIONS):
        global_result = _global_v49_calibration(config)
        candidate_multiplier = 1.0
        if str(config.get("opportunity_v4_strategy_version") or "").lower().startswith("v4.10"):
            direction_bias = str(global_result.get("global_direction_bias") or "NEUTRAL").upper()
            candidate_direction = str(candidate.get("direction") or "").upper()
            if direction_bias in {"LONG", "SHORT"} and candidate_direction in {"LONG", "SHORT"}:
                candidate_multiplier = (
                    float(config.get("opportunity_v410_direction_aligned_multiplier", 1.05))
                    if candidate_direction == direction_bias
                    else float(config.get("opportunity_v410_direction_countertrend_multiplier", 0.90))
                )
        return {
            **global_result,
            "schema": "adaptive_v410_global" if str(config.get("opportunity_v4_strategy_version") or "").lower().startswith("v4.10") else "adaptive_v49_global",
            "relation": "global",
            "direction": "GLOBAL",
            "market_regime": "global",
            "setup_type": "global",
            "risk_multiplier": round(float(global_result.get("risk_multiplier") or 1.0) * candidate_multiplier, 4),
            "prior_multiplier": candidate_multiplier,
            "evidence_delta": 0.0,
            "seed_version": None,
            "seed_weight": 0.0,
        }
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


def adaptive_calibration_status(config: dict[str, Any]) -> dict[str, Any]:
    """Return the current global V4.9 decision for Dashboard/API consumers."""
    version = str(config.get("opportunity_v4_strategy_version") or "")
    if not version.lower().startswith(GLOBAL_ADAPTIVE_VERSIONS):
        return {"enabled": False, "version": version, "scope": "inactive"}
    result = dict(_global_v49_calibration(config))
    if version.lower().startswith("v4.10"):
        result["schema"] = "adaptive_v410_global"
    return result
