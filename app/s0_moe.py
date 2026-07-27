from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.market_structure import market_structure, normalize_setup_type
from app.telemetry import connect, db_path
from app.strategy_capabilities import strategy_family_for_version
from app.training_lineage import capture_minute_features


DEFAULT_MODEL = Path(__file__).resolve().parent / "model_artifacts" / "s0_binance_moe_v1_5.joblib"
DEFAULT_CANDIDATE_STATUS = (
    Path(__file__).resolve().parent
    / "model_artifacts"
    / "s0_binance_moe_candidate_status.json"
)


def _float(value: Any, default: float = math.nan) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@lru_cache(maxsize=2)
def _load_bundle(path: str) -> dict[str, Any]:
    import joblib

    return joblib.load(path)


def clear_moe_cache() -> None:
    _load_bundle.cache_clear()
    _online_evidence_cached.cache_clear()


def _model_path(config: dict[str, Any]) -> Path:
    configured = str(config.get("s0_moe_model_path") or "").strip()
    return Path(configured) if configured else DEFAULT_MODEL


def _candidate_status(config: dict[str, Any]) -> dict[str, Any]:
    configured = str(config.get("s0_moe_candidate_status_path") or "").strip()
    path = Path(configured) if configured else DEFAULT_CANDIDATE_STATUS
    if not path.exists():
        return {
            "version": "s0_binance_moe_v1_5",
            "decision": "not_trained",
            "reason": "尚未生成 MoE v1.5 混合权重候选报告",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {
            "version": "s0_binance_moe_v1_5",
            "decision": "status_unreadable",
            "reason": "MoE v1.5 候选报告无法读取",
        }


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _evidence_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [row for row in rows if str(row.get("status") or "").upper() == "CLOSED"]
    gains = sum(max(0.0, _float(row.get("net_pnl"), 0.0)) for row in closed)
    losses = -sum(min(0.0, _float(row.get("net_pnl"), 0.0)) for row in closed)
    wins = sum(_float(row.get("net_pnl"), 0.0) > 0 for row in closed)
    return {
        "total": len(rows),
        "open": len(rows) - len(closed),
        "closed": len(closed),
        "opportunities": len(
            {str(row.get("opportunity_id") or "") for row in rows if row.get("opportunity_id")}
        ),
        "symbols": len({str(row.get("symbol") or "") for row in rows if row.get("symbol")}),
        "regimes": len(
            {str(row.get("market_regime") or "") for row in rows if row.get("market_regime")}
        ),
        "wins": wins,
        "win_rate": round(wins / len(closed) * 100.0, 4) if closed else None,
        "net_pnl": round(sum(_float(row.get("net_pnl"), 0.0) for row in closed), 8),
        "estimated_cost": round(
            sum(_float(row.get("estimated_cost"), 0.0) for row in closed),
            8,
        ),
        "profit_factor": (
            round(gains / losses, 4)
            if losses > 0
            else (999.0 if gains > 0 else None)
        ),
    }


def _aggregate_online_evidence(
    rows: list[dict[str, Any]],
    *,
    cutoff: datetime | None = None,
) -> dict[str, Any]:
    evaluated: list[dict[str, Any]] = []
    active_gate: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    by_expert: dict[str, list[dict[str, Any]]] = {}
    last_evaluated_at: datetime | None = None
    for row in rows:
        opened_at = _parse_time(row.get("opened_at"))
        if cutoff and (not opened_at or opened_at < cutoff):
            continue
        payload = row.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                continue
        moe = (payload or {}).get("moe") or {}
        if not moe.get("enabled"):
            continue
        item = {**row, "_moe": moe}
        evaluated.append(item)
        if opened_at and (last_evaluated_at is None or opened_at > last_evaluated_at):
            last_evaluated_at = opened_at
        if moe.get("active_gate"):
            active_gate.append(item)
        if moe.get("passed"):
            selected.append(item)
            expert = str(moe.get("expert") or "unknown")
            by_expert.setdefault(expert, []).append(item)
    return {
        "evaluated": _evidence_metrics(evaluated),
        "active_gate": _evidence_metrics(active_gate),
        "selected": _evidence_metrics(selected),
        "by_expert": {
            name: _evidence_metrics(items)
            for name, items in sorted(by_expert.items())
        },
        "last_evaluated_at": last_evaluated_at.isoformat() if last_evaluated_at else None,
    }


@lru_cache(maxsize=16)
def _online_evidence_cached(
    database: str,
    strategy_family: str,
    strategy_version: str,
    model_version: str,
    window_hours: float,
    cache_bucket: int,
) -> dict[str, Any]:
    del database, cache_bucket
    try:
        with connect() as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT id, opened_at, closed_at, symbol, direction, status, net_pnl,
                           estimated_cost, opportunity_id, market_regime, payload
                    FROM shadow_trades
                    WHERE strategy_family = ? AND strategy_version = ?
                      AND payload LIKE ?
                    ORDER BY id DESC LIMIT 50000
                    """,
                    (strategy_family, strategy_version, f'%"version":"{model_version}"%'),
                ).fetchall()
            ]
    except Exception as exc:
        return {"available": False, "error": type(exc).__name__}
    now = datetime.now(timezone.utc)
    return {
        "available": True,
        "strategy_version": strategy_version,
        "model_version": model_version,
        "current_version": _aggregate_online_evidence(rows),
        "rolling_window": {
            "hours": window_hours,
            **_aggregate_online_evidence(
                rows,
                cutoff=now - timedelta(hours=window_hours),
            ),
        },
    }


def online_moe_evidence(config: dict[str, Any], model_version: str) -> dict[str, Any]:
    strategy_version = str(config.get("opportunity_v4_strategy_version") or "unknown")
    strategy_family = strategy_family_for_version(strategy_version)
    window_hours = float(config.get("s0_moe_online_window_hours", 24.0))
    return _online_evidence_cached(
        str(db_path().resolve()),
        strategy_family,
        strategy_version,
        model_version,
        window_hours,
        int(time.time() // 30),
    )


def _directional(value: float, direction: str) -> float:
    return value if direction == "LONG" else -value


def _micro_features(candidate: dict[str, Any], direction: str) -> dict[str, float]:
    captured = capture_minute_features(candidate)
    rows = list((captured.get("bars_1m") or {}).get("rows") or [])
    if len(rows) < 2:
        return {}
    closes = [_float(row[4]) for row in rows if len(row) > 10]
    opens = [_float(row[1]) for row in rows if len(row) > 10]
    highs = [_float(row[2]) for row in rows if len(row) > 10]
    lows = [_float(row[3]) for row in rows if len(row) > 10]
    quotes = [_float(row[7], 0.0) for row in rows if len(row) > 10]
    trades = [_float(row[8], 0.0) for row in rows if len(row) > 10]
    taker_quotes = [_float(row[10], 0.0) for row in rows if len(row) > 10]
    if len(closes) < 2:
        return {}
    returns = [
        _directional((closes[index] / closes[index - 1] - 1.0) * 100.0, direction)
        for index in range(1, len(closes))
        if closes[index - 1] > 0
    ]
    raw_returns = [
        (closes[index] / closes[index - 1] - 1.0) * 100.0
        for index in range(1, len(closes))
        if closes[index - 1] > 0
    ]
    realized = sum(abs(value) for value in raw_returns[-5:])
    net = abs(sum(raw_returns[-5:]))
    imbalances = [
        ((2.0 * taker / quote) - 1.0) if quote > 0 else 0.0
        for taker, quote in zip(taker_quotes, quotes)
    ]
    last = len(closes) - 1
    candle_range = max(highs[last] - lows[last], 1e-12)
    adverse_wick = (
        (min(opens[last], closes[last]) - lows[last]) / candle_range
        if direction == "LONG"
        else (highs[last] - max(opens[last], closes[last])) / candle_range
    )
    close_location = (
        (closes[last] - lows[last]) / candle_range
        if direction == "LONG"
        else (highs[last] - closes[last]) / candle_range
    )
    quote_base = sum(quotes[-6:-1]) / max(len(quotes[-6:-1]), 1)
    trade_base = sum(trades[-6:-1]) / max(len(trades[-6:-1]), 1)
    return {
        "micro_ret_1_dir": returns[-1] if returns else math.nan,
        "micro_ret_3_dir": sum(returns[-3:]),
        "micro_ret_5_dir": sum(returns[-5:]),
        "micro_path_efficiency": net / realized if realized > 0 else 0.0,
        "micro_directional_minutes": sum(1 for value in returns[-5:] if value > 0),
        "micro_realized_vol": realized,
        "micro_max_impulse": max([abs(value) for value in raw_returns[-5:]] or [0.0]),
        "micro_adverse_wick": adverse_wick,
        "micro_close_location": close_location,
        "micro_taker_imbalance": sum(imbalances[-5:]) / max(len(imbalances[-5:]), 1),
        "micro_taker_imbalance_last": imbalances[-1] if imbalances else 0.0,
        "micro_taker_imbalance_slope": (
            imbalances[-1] - imbalances[-4] if len(imbalances) >= 4 else 0.0
        ),
        "micro_taker_persistence": sum(1 for value in imbalances[-5:] if value > 0),
        "micro_quote_acceleration": quotes[-1] / quote_base if quote_base > 0 else 1.0,
        "micro_trade_acceleration": trades[-1] / trade_base if trade_base > 0 else 1.0,
        "micro_volume_concentration": quotes[-1] / max(sum(quotes[-5:]), 1e-12),
    }


def _feature_row(candidate: dict[str, Any]) -> dict[str, Any]:
    v4 = candidate.get("opportunity_v4") or {}
    features = v4.get("features") or {}
    signal = candidate.get("signal") or {}
    depth = candidate.get("depth") or {}
    smart_flow = candidate.get("smart_flow") or {}
    structure = market_structure(candidate)
    direction = str(candidate.get("direction") or "").upper()
    regime = str(structure.get("market_regime") or "unknown").lower()
    returns = list(signal.get("multi_horizon_returns") or [])

    def horizon(index: int) -> float:
        return _float(returns[index]) if len(returns) > index else math.nan

    ret_1, ret_3, ret_12, ret_48 = (horizon(index) for index in range(4))
    row = {
        "rank_percentile": _float(v4.get("rank_percentile")),
        "quality": _float(v4.get("model_quality") or v4.get("quality") or _float(v4.get("score")) / 100.0),
        "expected_net_pct": _float(v4.get("expected_net_pct")),
        "lower_expected_net_pct": _float(v4.get("lower_expected_net_pct")),
        "cost_ratio": _float(v4.get("cost_ratio") or candidate.get("cost_ratio")),
        "atr_pct": _float(signal.get("atr_pct")),
        "ret_1_dir": ret_1,
        "ret_3_dir": ret_3,
        "ret_6_dir": (ret_3 + ret_12) / 2.0,
        "ret_12_dir": ret_12,
        "ret_24_dir": (ret_12 + ret_48) / 2.0,
        "volume_acceleration": _float(signal.get("volume_acceleration")),
        "directional_flow": _float(signal.get("directed_trade_flow")),
        "path_efficiency": _float(structure.get("medium_path_efficiency")),
        "extension_atr": _float(signal.get("breakout_extension_atr")),
        "impulse_atr": _float(signal.get("impulse_atr")),
        "range_position_dir": math.nan,
        "quote_volume_1h_log": math.nan,
        "market_ret_dir": math.nan,
        "market_breadth_dir": math.nan,
        "market_atr_pct": math.nan,
        "volume_persistence": _float(features.get("volume_persistence")),
        "directed_flow": _float(features.get("directed_flow")),
        "medium_path": _float(features.get("medium_path")),
        "anti_chase": _float(features.get("anti_chase")),
        "regime_fit": _float(features.get("regime_fit")),
        "entry_quality": _float(features.get("entry_quality")),
        "medium_alignment": _float(features.get("medium_alignment")),
        "liquidity": _float(features.get("liquidity")),
        "smart_flow_alignment": _float(
            features.get("smart_flow_alignment")
            if features.get("smart_flow_alignment") is not None
            else smart_flow.get("directional_alignment")
        ),
        "spread_pct": _float(depth.get("spread_pct")),
        "depth_log": (
            math.log1p(max(_float(depth.get("depth_notional"), 0.0), 0.0))
            if depth.get("depth_notional") is not None
            else math.nan
        ),
        "confirmations": _float(v4.get("confirmations") or v4.get("v44_confirmations")),
        "direction": direction,
        "market_regime": regime,
    }
    row.update(_micro_features(candidate, direction))
    return row


def evaluate_candidate(candidate: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    if not config.get("s0_moe_shadow_enabled", True):
        return {"enabled": False, "mode": "shadow_only", "reason": "disabled"}
    if not config.get("s0_moe_runtime_model_enabled", False):
        return {
            "enabled": False,
            "mode": "shadow_only",
            "reason": "runtime_model_retired",
            "affects_live_admission": False,
        }
    path = _model_path(config)
    if not path.exists():
        return {"enabled": False, "mode": "shadow_only", "reason": "model_missing"}
    try:
        import pandas as pd

        bundle = _load_bundle(str(path))
        structure = market_structure(candidate)
        setup_name = normalize_setup_type(
            structure.get("setup_type") or candidate.get("entry_type")
        )
        expert_name = str(
            (bundle.get("setup_aliases") or {}).get(setup_name)
            or setup_name
        )
        regime = str(structure.get("market_regime") or "unknown").lower()
        expert = (bundle.get("experts") or {}).get(expert_name)
        floor = (bundle.get("gate_floors") or {}).get(f"{expert_name}|{regime}")
        if not expert or floor is None:
            return {
                "enabled": True,
                "version": bundle.get("version"),
                "mode": "shadow_only",
                "setup_type": setup_name,
                "expert": expert_name,
                "market_regime": regime,
                "active_gate": False,
                "passed": False,
                "reason": "当前形态与市场状态没有可用的研究影子门槛",
            }
        row = _feature_row(candidate)
        frame = pd.DataFrame([row], columns=bundle["features"])
        categories = expert["classifier"].booster_.pandas_categorical or []
        for index, column in enumerate(bundle.get("categorical") or []):
            allowed = categories[index] if index < len(categories) else None
            frame[column] = pd.Categorical(frame[column], categories=allowed)
        probability = float(expert["classifier"].predict_proba(frame)[0, 1])
        predicted_net = float(expert["regressor"].predict(frame)[0])
        edge = predicted_net + (probability - 0.5) * 0.50
        passed = edge >= float(floor)
        return {
            "enabled": True,
            "version": bundle.get("version"),
            "mode": "shadow_only",
            "setup_type": setup_name,
            "expert": expert_name,
            "market_regime": regime,
            "active_gate": True,
            "predicted_win_rate": round(probability * 100, 4),
            "predicted_net_pct": round(predicted_net, 6),
            "model_edge": round(edge, 6),
            "edge_floor": round(float(floor), 6),
            "passed": passed,
            "reason": "模型建议记录为选中影子机会" if passed else "模型优势未达到研究影子门槛",
            "affects_live_admission": False,
            "rest_requests": 0,
        }
    except Exception as exc:
        return {
            "enabled": False,
            "mode": "shadow_only",
            "reason": f"inference_error:{type(exc).__name__}",
        }


def attach_moe_shadow(candidates: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    if not config.get("s0_moe_shadow_enabled", True) or not config.get(
        "s0_moe_runtime_model_enabled", False
    ):
        return candidates
    limit = max(1, int(config.get("s0_moe_shadow_candidate_limit", 25)))
    eligible = [
        candidate
        for candidate in candidates
        if (candidate.get("opportunity_v4") or {}).get("enabled")
        and str(candidate.get("direction") or "").upper() in {"LONG", "SHORT"}
    ]
    eligible.sort(
        key=lambda item: float((item.get("opportunity_v4") or {}).get("rank_percentile") or -1),
        reverse=True,
    )
    for candidate in eligible[:limit]:
        candidate.setdefault("opportunity_v4", {})["moe"] = evaluate_candidate(candidate, config)
    return candidates


def moe_runtime_status(config: dict[str, Any]) -> dict[str, Any]:
    path = _model_path(config)
    runtime_model_enabled = bool(config.get("s0_moe_runtime_model_enabled", False))
    base = {
        "enabled": bool(config.get("s0_moe_shadow_enabled", True)),
        "runtime_model_enabled": runtime_model_enabled,
        "runtime_status": "active" if runtime_model_enabled else "retired",
        "mode": "shadow_only",
        "affects_live_admission": False,
        "model_path": str(path),
        "model_exists": path.exists(),
        "candidate": _candidate_status(config),
        "candidate_limit": int(config.get("s0_moe_shadow_candidate_limit", 25)),
        "rest_requests": 0,
    }
    if not base["enabled"] or not path.exists():
        return base
    try:
        bundle = _load_bundle(str(path))
        model_version = str(bundle.get("version") or "unknown")
        online = online_moe_evidence(config, model_version)
        selected = ((online.get("current_version") or {}).get("selected") or {})
        min_closed = int(config.get("s0_moe_retrain_min_selected_closes", 200))
        min_regimes = int(config.get("s0_moe_retrain_min_regimes", 2))
        base.update(
            {
                "version": model_version,
                "decision": bundle.get("decision"),
                "active_gates": len(bundle.get("gate_floors") or {}),
                "training_summary": bundle.get("training_summary") or {},
                "online_shadow": online,
                "retraining": {
                    "mode": "controlled_snapshot",
                    "automatic_live_replacement": False,
                    "selected_closed": int(selected.get("closed") or 0),
                    "required_selected_closes": min_closed,
                    "selected_regimes": int(selected.get("regimes") or 0),
                    "required_regimes": min_regimes,
                    "ready": (
                        int(selected.get("closed") or 0) >= min_closed
                        and int(selected.get("regimes") or 0) >= min_regimes
                    ),
                },
            }
        )
    except Exception as exc:
        base["error"] = type(exc).__name__
    return base
