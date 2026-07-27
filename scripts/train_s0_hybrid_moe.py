from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import log_loss, mean_absolute_error, roc_auc_score


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_SOURCE = ROOT / "data" / "research" / "s0_public_1m" / "s0_candidates_1m.parquet"
VPS_HISTORY = ROOT / "data" / "research" / "vps_history_20260727"
MODEL_VERSION = "s0_binance_moe_v1_5"
OUTPUT = ROOT / "data" / "research" / MODEL_VERSION
ARTIFACT = ROOT / "app" / "model_artifacts" / f"{MODEL_VERSION}.joblib"
STATUS = ROOT / "app" / "model_artifacts" / "s0_binance_moe_candidate_status.json"
PUBLIC_TRAIN_END = pd.Timestamp("2026-07-13T00:00:00Z")
VALIDATION_START = pd.Timestamp("2026-07-24T00:00:00Z")
TEST_START = pd.Timestamp("2026-07-26T00:00:00Z")
EXPERTS = ("momentum", "breakout", "prebreakout", "pullback")
REGIMES = ("quiet", "rotation", "broad_up", "broad_down", "panic", "mixed", "unknown")
CATEGORICAL = ("direction", "market_regime")
NUMERIC = (
    "rank_percentile",
    "quality",
    "expected_net_pct",
    "lower_expected_net_pct",
    "cost_ratio",
    "volume_acceleration",
    "directional_flow",
    "path_efficiency",
    "extension_atr",
    "impulse_atr",
    "volume_persistence",
    "directed_flow",
    "medium_path",
    "medium_alignment",
    "anti_chase",
    "regime_fit",
    "entry_quality",
    "liquidity",
    "smart_flow_alignment",
    "spread_pct",
    "depth_log",
)
FEATURES = NUMERIC + CATEGORICAL


def _number(frame: pd.DataFrame, name: str, default: float = np.nan) -> pd.Series:
    if name not in frame:
        return pd.Series(default, index=frame.index, dtype=float)
    return pd.to_numeric(frame[name], errors="coerce")


def _normalize_setup(values: pd.Series) -> pd.Series:
    return (
        values.fillna("unknown")
        .astype(str)
        .str.lower()
        .replace(
            {
                "trend_breakout": "breakout",
                "breakout_test": "breakout",
                "pre_breakout": "prebreakout",
                "trend_pullback": "pullback",
                "pullback_probe": "pullback",
                "momentum_probe": "momentum",
            }
        )
    )


def _finish(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame[frame.setup_type.isin(EXPERTS)].copy()
    frame["win"] = (frame.net_pct > 0).astype(int)
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


def load_public(path: Path, max_rows_per_expert: int) -> pd.DataFrame:
    source = pd.read_parquet(path)
    source = source[source.minute_feature_valid].copy()
    source["time"] = pd.to_datetime(source.open_time, unit="ms", utc=True)
    source = source[source.time < PUBLIC_TRAIN_END]
    source["setup_type"] = _normalize_setup(source.setup_type)
    breakout = (
        source.setup_type.eq("momentum")
        & _number(source, "extension_atr").ge(0.15)
        & _number(source, "impulse_atr").ge(1.0)
    )
    source.loc[breakout, "setup_type"] = "breakout"
    frame = pd.DataFrame(index=source.index)
    frame["time"] = source.time
    frame["symbol"] = source.symbol.astype(str)
    frame["direction"] = source.direction
    frame["setup_type"] = source.setup_type
    frame["market_regime"] = source.market_regime
    frame["net_pct"] = _number(source, "net_pct_5m")
    frame["rank_percentile"] = _number(source, "rank_percentile")
    frame["quality"] = _number(source, "quality")
    frame["expected_net_pct"] = _number(source, "expected_net_pct")
    frame["lower_expected_net_pct"] = _number(source, "lower_expected_net_pct")
    frame["cost_ratio"] = _number(source, "cost_ratio")
    frame["volume_acceleration"] = _number(source, "volume_acceleration")
    frame["directional_flow"] = _number(source, "directional_flow")
    frame["path_efficiency"] = _number(source, "path_efficiency")
    frame["extension_atr"] = _number(source, "extension_atr")
    frame["impulse_atr"] = _number(source, "impulse_atr")
    for column in (
        "volume_persistence",
        "directed_flow",
        "medium_path",
        "medium_alignment",
        "anti_chase",
        "regime_fit",
        "entry_quality",
    ):
        frame[column] = _number(source, column)
    frame["liquidity"] = np.nan
    frame["smart_flow_alignment"] = np.nan
    frame["spread_pct"] = np.nan
    frame["depth_log"] = np.nan
    frame["source"] = "binance_public"
    frame["base_weight"] = 0.12
    frame["opportunity_id"] = (
        frame.symbol + ":" + frame.direction.astype(str) + ":" + source.open_time.astype(str)
    )
    parts = []
    for index, setup in enumerate(EXPERTS):
        scoped = frame[frame.setup_type.eq(setup)]
        if len(scoped) > max_rows_per_expert:
            scoped = scoped.sample(max_rows_per_expert, random_state=715 + index)
        parts.append(scoped)
    return _finish(pd.concat(parts).sort_values("time"))


def load_shadow(history: Path) -> pd.DataFrame:
    source = pd.read_csv(history / "shadow_trades_compact.csv.gz", low_memory=False)
    source = source[
        source.strategy_family.eq("extreme_v4_roll")
        & source.status.eq("CLOSED")
        & source.evidence_type.eq("decision")
        & source.opportunity_id.notna()
    ].copy()
    frame = pd.DataFrame(index=source.index)
    frame["time"] = pd.to_datetime(source.opened_at, utc=True)
    frame["symbol"] = source.symbol.astype(str)
    frame["direction"] = source.direction
    frame["setup_type"] = _normalize_setup(source.setup_type)
    frame["market_regime"] = source.market_regime
    frame["net_pct"] = (
        _number(source, "net_pnl").div(_number(source, "notional")).mul(100.0)
    )
    frame["rank_percentile"] = _number(source, "rank_percentile")
    frame["quality"] = _number(source, "opportunity_score").div(100.0)
    frame["expected_net_pct"] = _number(source, "expected_net_pct")
    frame["lower_expected_net_pct"] = _number(source, "lower_expected_net_pct")
    frame["cost_ratio"] = _number(source, "cost_ratio")
    frame["volume_acceleration"] = _number(source, "volume_acceleration")
    frame["directional_flow"] = _number(source, "directed_trade_flow")
    frame["path_efficiency"] = _number(source, "medium_path_efficiency")
    frame["extension_atr"] = _number(source, "breakout_extension_atr")
    frame["impulse_atr"] = _number(source, "impulse_atr")
    for column in (
        "volume_persistence",
        "directed_flow",
        "medium_path",
        "medium_alignment",
        "anti_chase",
        "regime_fit",
        "entry_quality",
        "liquidity",
        "smart_flow_alignment",
        "spread_pct",
    ):
        frame[column] = _number(source, column)
    frame["depth_log"] = np.log1p(_number(source, "depth_notional").clip(lower=0))
    frame["source"] = "vps_shadow"
    frame["base_weight"] = 1.0
    frame["opportunity_id"] = source.opportunity_id.astype(str)
    frame["strategy_version"] = source.strategy_version.astype(str)
    return _finish(frame)


def load_live(history: Path) -> pd.DataFrame:
    live = pd.read_csv(history / "live_trade_records_compact.csv.gz", low_memory=False)
    lineage = pd.read_csv(history / "opportunity_lineage_compact.csv.gz", low_memory=False)
    live = live[
        live.strategy_family.eq("extreme_v4_roll")
        & live.opportunity_id.notna()
        & live.open_notional.gt(0)
    ].copy()
    lineage = lineage.sort_values("updated_at").drop_duplicates("opportunity_id", keep="last")
    source = live.merge(lineage, on="opportunity_id", how="inner", suffixes=("_live", "_lineage"))
    frame = pd.DataFrame(index=source.index)
    frame["time"] = pd.to_datetime(source.open_time, unit="ms", utc=True)
    frame["symbol"] = source.symbol_live.astype(str)
    frame["direction"] = source.direction_live
    frame["setup_type"] = _normalize_setup(source.setup_type)
    frame["market_regime"] = source.market_regime
    frame["net_pct"] = _number(source, "net_pnl_live").div(
        _number(source, "open_notional")
    ).mul(100.0)
    frame["rank_percentile"] = _number(source, "mf_rank_percentile")
    frame["quality"] = _number(source, "decision_score").div(100.0)
    frame["expected_net_pct"] = _number(source, "mf_expected_net_pct")
    frame["lower_expected_net_pct"] = _number(source, "mf_lower_expected_net_pct")
    frame["cost_ratio"] = np.nan
    frame["volume_acceleration"] = _number(source, "mf_volume_acceleration")
    frame["directional_flow"] = _number(source, "mf_directed_trade_flow")
    frame["path_efficiency"] = _number(source, "mf_medium_path_efficiency")
    frame["extension_atr"] = _number(source, "mf_breakout_extension_atr")
    frame["impulse_atr"] = _number(source, "mf_impulse_atr")
    for column in (
        "volume_persistence",
        "directed_flow",
        "medium_path",
        "medium_alignment",
        "anti_chase",
        "regime_fit",
        "entry_quality",
        "liquidity",
        "smart_flow_alignment",
    ):
        frame[column] = np.nan
    frame["spread_pct"] = _number(source, "mf_spread_pct")
    frame["depth_log"] = np.log1p(_number(source, "mf_depth_notional").clip(lower=0))
    frame["source"] = "vps_live"
    frame["base_weight"] = 6.0
    frame["opportunity_id"] = source.opportunity_id.astype(str)
    frame["strategy_version"] = source.strategy_version_live.astype(str)
    return _finish(frame)


def _split(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    public = frame.source.eq("binance_public")
    train = frame[public | (frame.time < VALIDATION_START)].copy()
    validation = frame[
        ~public & (frame.time >= VALIDATION_START) & (frame.time < TEST_START)
    ].copy()
    test = frame[~public & (frame.time >= TEST_START)].copy()
    return train, validation, test


def _weights(frame: pd.DataFrame) -> pd.Series:
    if frame.empty:
        return pd.Series(dtype=float)
    time = frame.time.astype("int64")
    recency = 0.75 + 0.50 * (time - time.min()) / max(time.max() - time.min(), 1)
    return frame.base_weight.astype(float) * recency


def fit_expert(train: pd.DataFrame, validation: pd.DataFrame, seed: int) -> dict[str, Any]:
    classifier = lgb.LGBMClassifier(
        objective="binary",
        n_estimators=700,
        learning_rate=0.03,
        num_leaves=15,
        min_child_samples=250,
        subsample=0.82,
        colsample_bytree=0.82,
        reg_alpha=4.0,
        reg_lambda=18.0,
        random_state=seed,
        n_jobs=-1,
        verbosity=-1,
    )
    regressor = lgb.LGBMRegressor(
        objective="huber",
        alpha=0.70,
        n_estimators=700,
        learning_rate=0.03,
        num_leaves=15,
        min_child_samples=250,
        subsample=0.82,
        colsample_bytree=0.82,
        reg_alpha=4.0,
        reg_lambda=20.0,
        random_state=seed + 1,
        n_jobs=-1,
        verbosity=-1,
    )
    callbacks = [lgb.early_stopping(60, verbose=False)]
    classifier.fit(
        train[list(FEATURES)],
        train.win,
        sample_weight=_weights(train),
        eval_set=[(validation[list(FEATURES)], validation.win)],
        eval_metric="binary_logloss",
        callbacks=callbacks,
        categorical_feature=list(CATEGORICAL),
    )
    regressor.fit(
        train[list(FEATURES)],
        train.net_pct,
        sample_weight=_weights(train),
        eval_set=[(validation[list(FEATURES)], validation.net_pct)],
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
        output.loc[mask, "model_edge"] = predicted_net + (probability - 0.5) * 0.50
    return output


def gate_key(setup: str, regime: str) -> str:
    return f"{setup}|{regime}"


def research_floors(validation: pd.DataFrame) -> dict[str, float]:
    floors: dict[str, float] = {}
    for setup in EXPERTS:
        global_scope = validation[validation.setup_type.eq(setup)]
        if len(global_scope) < 20:
            continue
        global_floor = float(global_scope.model_edge.quantile(0.90))
        for regime in REGIMES:
            scoped = global_scope[global_scope.market_regime.astype(str).eq(regime)]
            floors[gate_key(setup, regime)] = (
                float(scoped.model_edge.quantile(0.90)) if len(scoped) >= 20 else global_floor
            )
    return floors


def schedule(frame: pd.DataFrame, floors: dict[str, float]) -> pd.DataFrame:
    if frame.empty:
        return frame
    scoped = frame.copy()
    scoped["floor"] = [
        floors.get(gate_key(str(setup), str(regime)), np.inf)
        for setup, regime in zip(scoped.setup_type, scoped.market_regime)
    ]
    scoped = scoped[scoped.model_edge.ge(scoped.floor)]
    if scoped.empty:
        return scoped
    scoped["batch"] = scoped.time.map(
        lambda value: int(value.timestamp() * 1_000) // (5 * 60_000)
    ).astype("int64")
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
        free_at = row.time + pd.Timedelta(minutes=10)
        symbol_free[key] = row.time + pd.Timedelta(minutes=30)
    return scoped.loc[selected].sort_values("time")


def metrics(frame: pd.DataFrame, name: str) -> dict[str, Any]:
    if frame.empty:
        return {"name": name, "trades": 0}
    values = frame.net_pct.astype(float)
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
                "net_pct_points": round(float(scoped.net_pct.sum()), 6),
            }
            for source, scoped in frame.groupby("source")
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Train a Binance-public plus VPS-weighted S0 MoE shadow challenger."
    )
    parser.add_argument("--public-source", type=Path, default=PUBLIC_SOURCE)
    parser.add_argument("--vps-history", type=Path, default=VPS_HISTORY)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--artifact", type=Path, default=ARTIFACT)
    parser.add_argument("--status", type=Path, default=STATUS)
    parser.add_argument("--max-public-per-expert", type=int, default=180_000)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    public = load_public(args.public_source, args.max_public_per_expert)
    shadow = load_shadow(args.vps_history)
    live = load_live(args.vps_history)
    combined = pd.concat([public, shadow, live], ignore_index=True, sort=False)
    train, validation, test = _split(combined)
    experts: dict[str, dict[str, Any]] = {}
    fit_report: dict[str, Any] = {}
    for index, setup in enumerate(EXPERTS):
        train_scope = train[train.setup_type.eq(setup)]
        validation_scope = validation[validation.setup_type.eq(setup)]
        if len(train_scope) < 1_000 or len(validation_scope) < 50:
            continue
        expert = fit_expert(train_scope, validation_scope, 715 + index * 10)
        experts[setup] = expert
        probability = expert["classifier"].predict_proba(
            validation_scope[list(FEATURES)]
        )[:, 1]
        predicted = expert["regressor"].predict(validation_scope[list(FEATURES)])
        fit_report[setup] = {
            "train_rows": int(len(train_scope)),
            "validation_rows": int(len(validation_scope)),
            "validation_auc": round(
                float(roc_auc_score(validation_scope.win, probability)), 6
            ),
            "validation_log_loss": round(
                float(log_loss(validation_scope.win, probability)), 6
            ),
            "validation_mae_pct": round(
                float(mean_absolute_error(validation_scope.net_pct, predicted)), 6
            ),
        }

    validation = predict(validation, experts)
    test = predict(test, experts)
    floors = research_floors(validation)
    selected_validation = schedule(validation, floors)
    selected_test = schedule(test, floors)
    validation_metrics = metrics(selected_validation, "hybrid_moe_validation")
    test_metrics = metrics(selected_test, "hybrid_moe_test")
    release_checks = {
        "validation_trades": validation_metrics.get("trades", 0) >= 40,
        "validation_pf": validation_metrics.get("profit_factor", 0.0) >= 1.15,
        "validation_net": validation_metrics.get("net_pct_points", 0.0) > 0,
        "test_trades": test_metrics.get("trades", 0) >= 30,
        "test_pf": test_metrics.get("profit_factor", 0.0) >= 1.10,
        "test_net": test_metrics.get("net_pct_points", 0.0) > 0,
        "test_regimes": test_metrics.get("regimes", 0) >= 2,
        "test_symbols": test_metrics.get("symbols", 0) >= 10,
    }
    eligible = all(release_checks.values())
    generated_at = pd.Timestamp.now(tz="UTC").isoformat()
    report = {
        "experiment": MODEL_VERSION,
        "generated_at": generated_at,
        "scope": "S0 entry-quality shadow challenger; Binance public plus VPS weighted evidence",
        "data": {
            "public_rows": int(len(public)),
            "shadow_rows": int(len(shadow)),
            "live_rows": int(len(live)),
            "train_rows": int(len(train)),
            "validation_rows": int(len(validation)),
            "test_rows": int(len(test)),
            "public_weight": 0.12,
            "shadow_weight": 1.0,
            "live_weight": 6.0,
            "public_train_end": PUBLIC_TRAIN_END.isoformat(),
            "validation_start": VALIDATION_START.isoformat(),
            "test_start": TEST_START.isoformat(),
        },
        "fit": fit_report,
        "validation": validation_metrics,
        "untouched_test": test_metrics,
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
            "validation": validation_metrics,
            "untouched_test": test_metrics,
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
            "混合权重模型通过独立时间测试，仅进入线上影子验证，不接管实盘。"
            if eligible
            else "最新独立时间测试未证明扣费后正期望，仅上线研究影子收集后续样本。"
        ),
        "data": report["data"],
        "validation": validation_metrics,
        "untouched_test": test_metrics,
        "release_checks": release_checks,
        "active_gates": len(floors),
        "affects_live_admission": False,
        "online_learning": "线上只推理并记录结果；下一次在本地使用新影子和实盘重新训练。",
    }
    args.status.write_text(
        json.dumps(status, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
