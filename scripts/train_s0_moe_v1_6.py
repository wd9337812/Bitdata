from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import log_loss, mean_absolute_error, roc_auc_score


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.train_s0_hybrid_moe import (  # noqa: E402
    CATEGORICAL,
    EXPERTS,
    FEATURES,
    NUMERIC,
    REGIMES,
    _finish,
    _weights,
    load_public,
    _normalize_setup,
)


MODEL_VERSION = "s0_binance_moe_v1_6"
PUBLIC_SOURCE = ROOT / "data" / "research" / "s0_public_1m" / "s0_candidates_1m.parquet"
VPS_HISTORY = ROOT / "data" / "research" / "vps_history_20260728_v5"
OUTPUT = ROOT / "data" / "research" / MODEL_VERSION
ARTIFACT = ROOT / "app" / "model_artifacts" / f"{MODEL_VERSION}.joblib"
STATUS = ROOT / "app" / "model_artifacts" / "s0_binance_moe_candidate_status.json"
STRESS_MULTIPLIERS = (1.0, 1.5, 2.0)


def _number(value: Any, default: float = math.nan) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else {}
    except (TypeError, ValueError):
        return {}


def _first(*values: Any, default: float = math.nan) -> Any:
    for value in values:
        if value is not None:
            if isinstance(value, float) and math.isnan(value):
                continue
            if value != "":
                return value
    return default


def _setup(value: Any) -> str:
    return _normalize_setup(pd.Series([str(value or "unknown")])).iloc[0]


def _base_row(
    *,
    source: str,
    base_weight: float,
    time: Any,
    symbol: Any,
    direction: Any,
    setup_type: Any,
    market_regime: Any,
    net_pct: float,
    gross_pct: float,
    cost_pct: float,
    opportunity_id: Any,
    rank_percentile: Any,
    score: Any,
    expected_net_pct: Any,
    lower_expected_net_pct: Any,
    cost_ratio: Any,
    model_features: dict[str, Any],
    features: dict[str, Any],
    spread_pct: Any,
    depth_notional: Any,
    evidence_type: str = "",
) -> dict[str, Any]:
    values = {
        "time": pd.to_datetime(time, utc=True),
        "symbol": str(symbol or "UNKNOWN"),
        "direction": str(direction or "UNKNOWN").upper(),
        "setup_type": _setup(setup_type),
        "market_regime": str(market_regime or "unknown").lower(),
        "net_pct": net_pct,
        "gross_pct": gross_pct,
        "cost_pct": cost_pct,
        "net_stress_1_0": gross_pct - cost_pct * 1.0,
        "net_stress_1_5": gross_pct - cost_pct * 1.5,
        "net_stress_2_0": gross_pct - cost_pct * 2.0,
        "rank_percentile": _number(rank_percentile),
        "quality": _number(score) / 100.0,
        "expected_net_pct": _number(expected_net_pct),
        "lower_expected_net_pct": _number(lower_expected_net_pct),
        "cost_ratio": _number(cost_ratio),
        "volume_acceleration": _number(
            _first(features.get("volume_acceleration"), model_features.get("volume_acceleration"))
        ),
        "directional_flow": _number(
            _first(
                features.get("directed_trade_flow"),
                model_features.get("directed_flow"),
            )
        ),
        "path_efficiency": _number(
            _first(
                model_features.get("medium_path"),
                features.get("medium_path_efficiency"),
            )
        ),
        "extension_atr": _number(features.get("breakout_extension_atr")),
        "impulse_atr": _number(features.get("impulse_atr")),
        "volume_persistence": _number(model_features.get("volume_persistence")),
        "directed_flow": _number(model_features.get("directed_flow")),
        "medium_path": _number(model_features.get("medium_path")),
        "medium_alignment": _number(model_features.get("medium_alignment")),
        "anti_chase": _number(model_features.get("anti_chase")),
        "regime_fit": _number(model_features.get("regime_fit")),
        "entry_quality": _number(model_features.get("entry_quality")),
        "liquidity": _number(
            _first(model_features.get("liquidity"), features.get("liquidity"))
        ),
        "smart_flow_alignment": _number(
            _first(
                model_features.get("smart_flow_alignment"),
                features.get("smart_flow_alignment"),
            )
        ),
        "spread_pct": _number(spread_pct),
        "depth_log": math.log1p(max(_number(depth_notional, 0.0), 0.0)),
        "source": source,
        "base_weight": base_weight,
        "opportunity_id": str(opportunity_id or ""),
        "evidence_type": evidence_type,
    }
    return values


def _deduplicate(frame: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    if frame.empty:
        return frame, 0
    priority = {"v5_live": 3.0, "v5_shadow_decision": 2.0, "v5_shadow_exploration": 1.0}
    scoped = frame.copy()
    scoped["_priority"] = scoped.source.map(priority).fillna(0.0)
    scoped = scoped.sort_values(["opportunity_id", "_priority", "time"])
    before = len(scoped)
    scoped = scoped.drop_duplicates("opportunity_id", keep="last")
    return scoped.drop(columns="_priority"), before - len(scoped)


def load_exact_v5(history: Path) -> pd.DataFrame:
    shadow = pd.read_csv(history / "shadow_v5.csv.gz", low_memory=False)
    live = pd.read_csv(history / "live_v5.csv.gz", low_memory=False)
    lineage = pd.read_csv(history / "lineage_v5.csv.gz", low_memory=False)
    shadow_rows: list[dict[str, Any]] = []
    for raw in shadow.to_dict("records"):
        payload = _payload(raw.get("payload"))
        structure = payload.get("market_structure") or {}
        model_features = payload.get("model_features") or {}
        features = payload.get("features") or {}
        notional = max(_number(raw.get("notional"), 0.0), 1e-12)
        net = _number(raw.get("net_pnl"), 0.0)
        gross = _number(raw.get("gross_pnl"), net)
        cost = _number(raw.get("estimated_cost"), 0.0)
        shadow_rows.append(
            _base_row(
                source=(
                    "v5_shadow_decision"
                    if raw.get("evidence_type") == "decision"
                    else "v5_shadow_exploration"
                ),
                base_weight=1.0 if raw.get("evidence_type") == "decision" else 0.7,
                time=raw.get("opened_at"),
                symbol=raw.get("symbol"),
                direction=raw.get("direction"),
                setup_type=_first(
                    structure.get("setup_type"),
                    payload.get("entry_type"),
                    raw.get("signal_type"),
                ),
                market_regime=_first(
                    structure.get("market_regime"),
                    raw.get("market_regime"),
                    default="unknown",
                ),
                net_pct=net / notional * 100.0,
                gross_pct=gross / notional * 100.0,
                cost_pct=cost / notional * 100.0,
                opportunity_id=raw.get("opportunity_id"),
                rank_percentile=_first(
                    payload.get("rank_percentile"),
                    raw.get("rank_percentile"),
                ),
                score=_first(payload.get("score"), raw.get("opportunity_score")),
                expected_net_pct=_first(
                    payload.get("expected_net_pct"),
                    raw.get("expected_net_pct"),
                ),
                lower_expected_net_pct=_first(
                    payload.get("lower_expected_net_pct"),
                    raw.get("lower_expected_net_pct"),
                ),
                cost_ratio=_first(payload.get("cost_ratio"), raw.get("cost_ratio")),
                model_features=model_features,
                features=features,
                spread_pct=_first(
                    (payload.get("liquidity_gate") or {}).get("spread_pct"),
                    raw.get("spread_pct"),
                    features.get("spread_pct"),
                ),
                depth_notional=_first(
                    (payload.get("liquidity_gate") or {}).get("depth_notional"),
                    raw.get("depth_notional"),
                    features.get("depth_notional"),
                ),
                evidence_type=str(raw.get("evidence_type") or ""),
            )
        )
    shadow_frame = pd.DataFrame(shadow_rows)

    shadow_by_opportunity = {
        str(row.opportunity_id): row
        for row in shadow_frame.sort_values("time").itertuples()
        if row.opportunity_id
    }
    lineage_by_opportunity = {
        str(raw.get("opportunity_id")): raw
        for raw in lineage.to_dict("records")
        if raw.get("opportunity_id")
    }
    live_rows: list[dict[str, Any]] = []
    for raw in live.to_dict("records"):
        opportunity_id = str(raw.get("opportunity_id") or "")
        shadow = shadow_by_opportunity.get(opportunity_id)
        lineage_row = lineage_by_opportunity.get(opportunity_id) or {}
        if shadow is not None:
            item = shadow._asdict()
            item.update(
                {
                    "source": "v5_live",
                    "base_weight": 6.0,
                    "net_pct": _number(raw.get("net_pnl"), 0.0)
                    / max(_number(raw.get("open_notional"), 0.0), 1e-12)
                    * 100.0,
                    "opportunity_id": opportunity_id,
                    "time": pd.to_datetime(raw.get("open_time"), unit="ms", utc=True),
                }
            )
            live_rows.append(item)
            continue
        notional = max(_number(raw.get("open_notional"), 0.0), 1e-12)
        net = _number(raw.get("net_pnl"), 0.0)
        commission = abs(_number(raw.get("commission"), 0.0))
        payload = _payload(lineage_row.get("decision_payload"))
        structure = payload.get("market_structure") or {}
        live_rows.append(
            _base_row(
                source="v5_live",
                base_weight=6.0,
                time=pd.to_datetime(raw.get("open_time"), unit="ms", utc=True),
                symbol=raw.get("symbol"),
                direction=raw.get("direction"),
                setup_type=_first(
                    structure.get("setup_type"),
                    lineage_row.get("setup_type"),
                    lineage_row.get("signal_type"),
                ),
                market_regime=_first(
                    structure.get("market_regime"),
                    lineage_row.get("market_regime"),
                    default="unknown",
                ),
                net_pct=net / notional * 100.0,
                gross_pct=(net + commission) / notional * 100.0,
                cost_pct=commission / notional * 100.0,
                opportunity_id=opportunity_id,
                rank_percentile=math.nan,
                score=math.nan,
                expected_net_pct=math.nan,
                lower_expected_net_pct=math.nan,
                cost_ratio=math.nan,
                model_features={},
                features={},
                spread_pct=math.nan,
                depth_notional=math.nan,
            )
        )
    live_frame = pd.DataFrame(live_rows)
    frame = pd.concat([shadow_frame, live_frame], ignore_index=True, sort=False)
    frame, removed = _deduplicate(frame)
    return _finish_v16(frame), removed


def _finish_v16(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame[frame.setup_type.isin(EXPERTS)].copy()
    frame["win"] = (frame.net_stress_1_5 > 0).astype(int)
    for column in NUMERIC:
        if column not in frame:
            frame[column] = np.nan
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["direction"] = pd.Categorical(
        frame.direction.fillna("UNKNOWN").astype(str).str.upper(),
        categories=["LONG", "SHORT", "UNKNOWN"],
    )
    frame["market_regime"] = pd.Categorical(
        frame.market_regime.fillna("unknown").astype(str).str.lower(),
        categories=list(REGIMES),
    )
    return frame.replace([np.inf, -np.inf], np.nan)


def split_data(
    public: pd.DataFrame,
    exact: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    exact = exact.sort_values("time").reset_index(drop=True)
    if len(exact) < 30:
        raise RuntimeError("exact V5 sample is too small for a time split")
    train_end = exact.time.iloc[max(0, int(len(exact) * 0.60) - 1)]
    validation_end = exact.time.iloc[max(0, int(len(exact) * 0.80) - 1)]
    train = pd.concat(
        [public, exact[exact.time <= train_end]],
        ignore_index=True,
        sort=False,
    )
    validation = exact[(exact.time > train_end) & (exact.time <= validation_end)].copy()
    test = exact[exact.time > validation_end].copy()
    return train, validation, test, {
        "exact_train_end": train_end.isoformat(),
        "exact_validation_end": validation_end.isoformat(),
        "public_rows_train_only": int(len(public)),
        "exact_rows": int(len(exact)),
    }


def fit_expert(train: pd.DataFrame, validation: pd.DataFrame, seed: int) -> dict[str, Any]:
    classifier = lgb.LGBMClassifier(
        objective="binary",
        n_estimators=500,
        learning_rate=0.025,
        num_leaves=11,
        min_child_samples=80,
        subsample=0.82,
        colsample_bytree=0.80,
        reg_alpha=6.0,
        reg_lambda=24.0,
        random_state=seed,
        n_jobs=-1,
        verbosity=-1,
    )
    regressor = lgb.LGBMRegressor(
        objective="huber",
        alpha=0.70,
        n_estimators=500,
        learning_rate=0.025,
        num_leaves=11,
        min_child_samples=80,
        subsample=0.82,
        colsample_bytree=0.80,
        reg_alpha=6.0,
        reg_lambda=28.0,
        random_state=seed + 1,
        n_jobs=-1,
        verbosity=-1,
    )
    callbacks = [lgb.early_stopping(50, verbose=False)]
    weights = _weights(train)
    classifier.fit(
        train[list(FEATURES)],
        train.win,
        sample_weight=weights,
        eval_set=[(validation[list(FEATURES)], validation.win)],
        eval_metric="binary_logloss",
        callbacks=callbacks,
        categorical_feature=list(CATEGORICAL),
    )
    regressor.fit(
        train[list(FEATURES)],
        train.net_stress_1_5,
        sample_weight=weights,
        eval_set=[(validation[list(FEATURES)], validation.net_stress_1_5)],
        eval_metric="l1",
        callbacks=callbacks,
        categorical_feature=list(CATEGORICAL),
    )
    return {"classifier": classifier, "regressor": regressor}


def predict(frame: pd.DataFrame, experts: dict[str, dict[str, Any]]) -> pd.DataFrame:
    output = frame.copy()
    output["pred_win"] = np.nan
    output["pred_net"] = np.nan
    output["model_edge"] = np.nan
    for setup, expert in experts.items():
        mask = output.setup_type.eq(setup)
        matrix = output.loc[mask, list(FEATURES)]
        if matrix.empty:
            continue
        probability = expert["classifier"].predict_proba(matrix)[:, 1]
        predicted_net = expert["regressor"].predict(matrix)
        output.loc[mask, "pred_win"] = probability
        output.loc[mask, "pred_net"] = predicted_net
        output.loc[mask, "model_edge"] = predicted_net + (probability - 0.5) * 0.25
    return output


def make_floors(validation: pd.DataFrame) -> dict[str, float]:
    floors: dict[str, float] = {}
    for setup in EXPERTS:
        global_scope = validation[validation.setup_type.eq(setup)]
        if len(global_scope) < 20:
            continue
        global_floor = float(global_scope.model_edge.quantile(0.70))
        for regime in REGIMES:
            scoped = global_scope[global_scope.market_regime.astype(str).eq(regime)]
            floor = (
                float(scoped.model_edge.quantile(0.70))
                if len(scoped) >= 20
                else global_floor
            )
            floors[f"{setup}|{regime}"] = floor
    return floors


def schedule(frame: pd.DataFrame, floors: dict[str, float]) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    scoped = frame.copy()
    scoped["floor"] = [
        floors.get(f"{str(setup)}|{str(regime)}", math.inf)
        for setup, regime in zip(scoped.setup_type, scoped.market_regime)
    ]
    scoped = scoped[scoped.model_edge.ge(scoped.floor)].copy()
    if scoped.empty:
        return scoped
    scoped["batch"] = (
        scoped.time.map(lambda value: int(value.timestamp() * 1_000) // 300_000)
        .astype("int64")
    )
    scoped = scoped.sort_values(
        ["batch", "model_edge", "rank_percentile"],
        ascending=[True, False, False],
    ).drop_duplicates("batch", keep="first")
    selected: list[int] = []
    free_at: pd.Timestamp | None = None
    symbol_free: dict[tuple[str, str], pd.Timestamp] = {}
    for row in scoped.sort_values("time").itertuples():
        key = (str(row.symbol), str(row.direction))
        if free_at is not None and row.time < free_at:
            continue
        if row.time < symbol_free.get(key, pd.Timestamp("1970-01-01", tz="UTC")):
            continue
        selected.append(row.Index)
        timestamp = pd.Timestamp(row.time)
        free_at = timestamp + pd.Timedelta(minutes=10)
        symbol_free[key] = timestamp + pd.Timedelta(minutes=30)
    return scoped.loc[selected].sort_values("time")


def metrics(frame: pd.DataFrame, value_column: str, name: str) -> dict[str, Any]:
    if frame.empty:
        return {"name": name, "trades": 0, "net_pct_points": 0.0}
    values = pd.to_numeric(frame[value_column], errors="coerce").fillna(0.0)
    gains = float(values[values > 0].sum())
    losses = float(-values[values < 0].sum())
    curve = values.cumsum()
    drawdown = curve.cummax() - curve
    return {
        "name": name,
        "trades": int(len(frame)),
        "symbols": int(frame.symbol.nunique()),
        "regimes": int(frame.market_regime.nunique()),
        "win_rate": round(float((values > 0).mean() * 100.0), 4),
        "net_pct_points": round(float(values.sum()), 6),
        "mean_net_pct": round(float(values.mean()), 6),
        "profit_factor": round(gains / losses, 4) if losses else (999.0 if gains else 0.0),
        "max_drawdown_pct_points": round(float(drawdown.max()), 6),
        "by_source": {
            source: {
                "trades": int(len(scoped)),
                "net_pct_points": round(float(scoped[value_column].sum()), 6),
            }
            for source, scoped in frame.groupby("source")
        },
    }


def baseline_metrics(exact: pd.DataFrame) -> dict[str, Any]:
    return {
        "all_exact": metrics(exact, "net_stress_1_0", "v5_exact_all"),
        "decision": metrics(
            exact[exact.source.eq("v5_shadow_decision")],
            "net_stress_1_0",
            "v5_decision_all",
        ),
        "exploration": metrics(
            exact[exact.source.eq("v5_shadow_exploration")],
            "net_stress_1_0",
            "v5_exploration_all",
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Train an exact-V5, deduplicated, cost-stressed S0 MoE challenger."
    )
    parser.add_argument("--public-source", type=Path, default=PUBLIC_SOURCE)
    parser.add_argument("--vps-history", type=Path, default=VPS_HISTORY)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--artifact", type=Path, default=ARTIFACT)
    parser.add_argument("--status", type=Path, default=STATUS)
    parser.add_argument("--max-public-per-expert", type=int, default=60_000)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    public = load_public(args.public_source, args.max_public_per_expert)
    public["base_weight"] = 0.05
    public["net_stress_1_5"] = public["net_pct"]
    public["net_stress_1_0"] = public["net_pct"]
    public["net_stress_2_0"] = public["net_pct"]
    public["gross_pct"] = public["net_pct"]
    public["cost_pct"] = 0.0
    exact, deduplicated = load_exact_v5(args.vps_history)
    train, validation, test, split = split_data(public, exact)

    experts: dict[str, dict[str, Any]] = {}
    fit_report: dict[str, Any] = {}
    for index, setup in enumerate(EXPERTS):
        train_scope = train[train.setup_type.eq(setup)]
        validation_scope = validation[validation.setup_type.eq(setup)]
        if len(train_scope) < 250 or len(validation_scope) < 20:
            continue
        expert = fit_expert(train_scope, validation_scope, 1616 + index * 17)
        experts[setup] = expert
        probability = expert["classifier"].predict_proba(
            validation_scope[list(FEATURES)]
        )[:, 1]
        predicted = expert["regressor"].predict(validation_scope[list(FEATURES)])
        auc = (
            float(roc_auc_score(validation_scope.win, probability))
            if validation_scope.win.nunique() > 1
            else None
        )
        fit_report[setup] = {
            "train_rows": int(len(train_scope)),
            "validation_rows": int(len(validation_scope)),
            "validation_auc": round(auc, 6) if auc is not None else None,
            "validation_log_loss": round(
                float(log_loss(validation_scope.win, probability, labels=[0, 1])), 6
            ),
            "validation_mae_pct": round(
                float(mean_absolute_error(validation_scope.net_stress_1_5, predicted)), 6
            ),
        }

    validation = predict(validation, experts)
    test = predict(test, experts)
    floors = make_floors(validation)
    selected_validation = schedule(validation, floors)
    selected_test = schedule(test, floors)
    validation_metrics = metrics(selected_validation, "net_stress_1_0", "moe_v16_validation")
    test_metrics = metrics(selected_test, "net_stress_1_0", "moe_v16_test")
    stress_metrics = {
        f"{multiplier:.1f}x": {
            "validation": metrics(
                selected_validation,
                "net_stress_1_0" if multiplier == 1.0 else f"net_stress_{str(multiplier).replace('.', '_')}",
                f"v16_validation_cost_{multiplier:.1f}x",
            ),
            "test": metrics(
                selected_test,
                "net_stress_1_0" if multiplier == 1.0 else f"net_stress_{str(multiplier).replace('.', '_')}",
                f"v16_test_cost_{multiplier:.1f}x",
            ),
        }
        for multiplier in STRESS_MULTIPLIERS
    }
    baseline = baseline_metrics(exact)
    release_checks = {
        "validation_trades": validation_metrics.get("trades", 0) >= 20,
        "validation_net_after_1_5x_cost": stress_metrics["1.5x"]["validation"]["net_pct_points"] > 0,
        "test_trades": test_metrics.get("trades", 0) >= 15,
        "test_net_after_1_5x_cost": stress_metrics["1.5x"]["test"]["net_pct_points"] > 0,
        "test_pf_after_1_5x_cost": stress_metrics["1.5x"]["test"].get("profit_factor", 0.0) >= 1.05,
        "test_regimes": test_metrics.get("regimes", 0) >= 2,
        "test_symbols": test_metrics.get("symbols", 0) >= 8,
        "not_worse_than_v5_all_at_1_5x": (
            stress_metrics["1.5x"]["test"]["net_pct_points"]
            >= baseline["all_exact"]["net_pct_points"] / max(len(exact), 1) * max(test_metrics.get("trades", 0), 1)
        ),
    }
    eligible = all(release_checks.values())
    generated_at = pd.Timestamp.now(tz="UTC").isoformat()
    report = {
        "experiment": MODEL_VERSION,
        "generated_at": generated_at,
        "scope": "S0 exact V5 lineage only; public Binance data is train-only low-weight support",
        "data": {
            "public_rows": int(len(public)),
            "exact_v5_rows_before_dedup": int(len(exact) + deduplicated),
            "exact_v5_rows_after_dedup": int(len(exact)),
            "deduplicated_rows": int(deduplicated),
            "v5_sources": exact.source.value_counts().to_dict(),
            "public_weight": 0.05,
            "v5_decision_weight": 1.0,
            "v5_exploration_weight": 0.7,
            "v5_live_weight": 6.0,
            "stress_cost_multipliers": list(STRESS_MULTIPLIERS),
            "split": split,
        },
        "fit": fit_report,
        "baseline": baseline,
        "validation": validation_metrics,
        "untouched_test": test_metrics,
        "cost_stress": stress_metrics,
        "release_checks": release_checks,
        "active_research_gates": len(floors),
        "decision": "shadow_candidate" if eligible else "research_shadow_only",
        "affects_live_admission": False,
    }
    bundle = {
        "version": MODEL_VERSION,
        "setup_aliases": {name: name for name in EXPERTS},
        "experts": experts,
        "gate_floors": floors,
        "features": list(FEATURES),
        "categorical": list(CATEGORICAL),
        "decision": report["decision"],
        "affects_live_admission": False,
        "training_summary": {
            "generated_at": generated_at,
            "data": report["data"],
            "baseline": baseline,
            "validation": validation_metrics,
            "untouched_test": test_metrics,
            "cost_stress": stress_metrics,
            "release_checks": release_checks,
        },
    }
    args.artifact.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, args.artifact, compress=3)
    joblib.dump(bundle, args.output / f"{MODEL_VERSION}.joblib", compress=3)
    (args.output / f"{MODEL_VERSION}_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    status = {
        "version": MODEL_VERSION,
        "generated_at": generated_at,
        "decision": report["decision"],
        "reason": (
            "精确 V5 谱系通过时间切分和 1.5 倍成本压力测试，先进入独立线上影子，仍不接管实盘。"
            if eligible
            else "精确 V5 谱系的样本外扣费后证据未达到放行标准，只保留研究影子，不接管实盘。"
        ),
        "data": report["data"],
        "baseline": baseline,
        "validation": validation_metrics,
        "untouched_test": test_metrics,
        "cost_stress": stress_metrics,
        "release_checks": release_checks,
        "active_gates": len(floors),
        "affects_live_admission": False,
        "online_learning": "线上只推理并记录；下一次训练仍从隔离的 V5.0-S30 实盘与影子数据开始。",
    }
    args.status.write_text(
        json.dumps(status, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
