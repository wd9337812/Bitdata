from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.market_structure import market_structure, normalize_setup_type
from app.training_lineage import capture_minute_features


DEFAULT_MODEL = Path(__file__).resolve().parent / "model_artifacts" / "s0_binance_moe_v1.joblib"


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


def _model_path(config: dict[str, Any]) -> Path:
    configured = str(config.get("s0_moe_model_path") or "").strip()
    return Path(configured) if configured else DEFAULT_MODEL


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
        "confirmations": _float(v4.get("confirmations") or v4.get("v44_confirmations")),
        "direction": direction,
        "market_regime": regime,
    }
    row.update(_micro_features(candidate, direction))
    return row


def evaluate_candidate(candidate: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    if not config.get("s0_moe_shadow_enabled", True):
        return {"enabled": False, "mode": "shadow_only", "reason": "disabled"}
    path = _model_path(config)
    if not path.exists():
        return {"enabled": False, "mode": "shadow_only", "reason": "model_missing"}
    try:
        import pandas as pd

        bundle = _load_bundle(str(path))
        structure = market_structure(candidate)
        expert_name = normalize_setup_type(structure.get("setup_type") or candidate.get("entry_type"))
        regime = str(structure.get("market_regime") or "unknown").lower()
        expert = (bundle.get("experts") or {}).get(expert_name)
        floor = (bundle.get("gate_floors") or {}).get(f"{expert_name}|{regime}")
        if not expert or floor is None:
            return {
                "enabled": True,
                "version": bundle.get("version"),
                "mode": "shadow_only",
                "expert": expert_name,
                "market_regime": regime,
                "active_gate": False,
                "passed": False,
                "reason": "当前形态与市场状态没有通过样本外验证",
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
            "expert": expert_name,
            "market_regime": regime,
            "active_gate": True,
            "predicted_win_rate": round(probability * 100, 4),
            "predicted_net_pct": round(predicted_net, 6),
            "model_edge": round(edge, 6),
            "edge_floor": round(float(floor), 6),
            "passed": passed,
            "reason": "模型建议记录影子机会" if passed else "模型优势未达到独立验证门槛",
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
    if not config.get("s0_moe_shadow_enabled", True):
        return candidates
    limit = max(1, int(config.get("s0_moe_shadow_candidate_limit", 10)))
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
    base = {
        "enabled": bool(config.get("s0_moe_shadow_enabled", True)),
        "mode": "shadow_only",
        "affects_live_admission": False,
        "model_path": str(path),
        "model_exists": path.exists(),
        "candidate_limit": int(config.get("s0_moe_shadow_candidate_limit", 10)),
        "rest_requests": 0,
    }
    if not base["enabled"] or not path.exists():
        return base
    try:
        bundle = _load_bundle(str(path))
        base.update(
            {
                "version": bundle.get("version"),
                "decision": bundle.get("decision"),
                "active_gates": len(bundle.get("gate_floors") or {}),
                "training_summary": bundle.get("training_summary") or {},
            }
        )
    except Exception as exc:
        base["error"] = type(exc).__name__
    return base
