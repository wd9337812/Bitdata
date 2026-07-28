from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

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
    REGIMES,
    _weights,
    load_public,
)
from scripts.train_s0_moe_v1_6 import (  # noqa: E402
    STRESS_MULTIPLIERS,
    baseline_metrics,
    fit_expert,
    load_exact_v5,
    make_floors,
    metrics,
    predict,
)


MODEL_VERSION = "s0_binance_moe_v1_7"
PUBLIC_SOURCE = ROOT / "data" / "research" / "s0_public_1m" / "s0_candidates_1m.parquet"
VPS_HISTORY = ROOT / "data" / "research" / "vps_history_20260728_v5"
OUTPUT = ROOT / "data" / "research" / MODEL_VERSION
ARTIFACT = ROOT / "app" / "model_artifacts" / f"{MODEL_VERSION}.joblib"
STATUS = ROOT / "app" / "model_artifacts" / "s0_binance_moe_candidate_status.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Train an event-group isolated S0 MoE research challenger."
    )
    parser.add_argument("--public-source", type=Path, default=PUBLIC_SOURCE)
    parser.add_argument("--vps-history", type=Path, default=VPS_HISTORY)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--artifact", type=Path, default=ARTIFACT)
    parser.add_argument("--status", type=Path, default=STATUS)
    parser.add_argument("--max-public-per-expert", type=int, default=60_000)
    return parser.parse_args()


def attach_event_groups(exact: pd.DataFrame, history: Path) -> pd.DataFrame:
    output = exact.copy()
    lineage_path = history / "lineage_v5.csv.gz"
    mapping: dict[str, str] = {}
    if lineage_path.exists():
        lineage = pd.read_csv(
            lineage_path,
            usecols=lambda name: name in {"opportunity_id", "event_group_id", "event_id"},
            low_memory=False,
        )
        for raw in lineage.to_dict("records"):
            opportunity_id = str(raw.get("opportunity_id") or "")
            event_group_id = str(raw.get("event_group_id") or raw.get("event_id") or "")
            if opportunity_id and event_group_id and event_group_id.lower() != "nan":
                mapping[opportunity_id] = event_group_id
    output["event_group_id"] = [
        mapping.get(str(opportunity_id), str(opportunity_id))
        for opportunity_id in output.opportunity_id
    ]
    empty = output.event_group_id.astype(str).isin({"", "nan", "None"})
    output.loc[empty, "event_group_id"] = output.loc[empty, "opportunity_id"].astype(str)
    return output


def cap_event_group_weights(exact: pd.DataFrame) -> pd.DataFrame:
    """Keep repeated shadows useful without letting one market wave dominate."""
    output = exact.copy()
    group_weight = output.groupby("event_group_id").base_weight.transform("sum").clip(lower=1.0)
    source_cap = output.source.map(
        {
            "v5_live": 6.0,
            "v5_shadow_decision": 1.5,
            "v5_shadow_exploration": 1.0,
        }
    ).fillna(1.0)
    output["base_weight"] = np.minimum(
        output.base_weight,
        source_cap / group_weight * output.base_weight,
    )
    return output


def group_time_split(
    public: pd.DataFrame,
    exact: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    groups = (
        exact.groupby("event_group_id", as_index=False)
        .agg(first_time=("time", "min"))
        .sort_values("first_time")
        .reset_index(drop=True)
    )
    if len(groups) < 30:
        raise RuntimeError("independent V5 event groups are too few for a time split")
    train_count = max(1, int(len(groups) * 0.60))
    validation_count = max(1, int(len(groups) * 0.20))
    train_groups = set(groups.iloc[:train_count].event_group_id)
    validation_groups = set(
        groups.iloc[train_count : train_count + validation_count].event_group_id
    )
    test_groups = set(groups.iloc[train_count + validation_count :].event_group_id)
    train = pd.concat(
        [public, exact[exact.event_group_id.isin(train_groups)]],
        ignore_index=True,
        sort=False,
    )
    validation = exact[exact.event_group_id.isin(validation_groups)].copy()
    test = exact[exact.event_group_id.isin(test_groups)].copy()
    overlap = {
        "train_validation": len(train_groups & validation_groups),
        "train_test": len(train_groups & test_groups),
        "validation_test": len(validation_groups & test_groups),
    }
    return train, validation, test, {
        "event_groups": int(len(groups)),
        "train_event_groups": int(len(train_groups)),
        "validation_event_groups": int(len(validation_groups)),
        "test_event_groups": int(len(test_groups)),
        "group_overlap": overlap,
        "public_rows_train_only": int(len(public)),
        "exact_rows": int(len(exact)),
    }


def event_group_schedule(frame: pd.DataFrame, floors: dict[str, float]) -> pd.DataFrame:
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
    scoped = (
        scoped.sort_values(
            ["event_group_id", "model_edge", "rank_percentile"],
            ascending=[True, False, False],
        )
        .drop_duplicates("event_group_id", keep="first")
        .sort_values("time")
    )
    selected: list[int] = []
    free_at: pd.Timestamp | None = None
    symbol_free: dict[tuple[str, str], pd.Timestamp] = {}
    for row in scoped.itertuples():
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


def main() -> None:
    import joblib

    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    public = load_public(args.public_source, args.max_public_per_expert)
    public["base_weight"] = 0.05
    public["event_group_id"] = [
        f"public:{index}" for index in range(len(public))
    ]
    for column in ("net_stress_1_0", "net_stress_1_5", "net_stress_2_0"):
        public[column] = public["net_pct"]
    public["gross_pct"] = public["net_pct"]
    public["cost_pct"] = 0.0

    exact, deduplicated = load_exact_v5(args.vps_history)
    exact = attach_event_groups(exact, args.vps_history)
    exact = cap_event_group_weights(exact)
    train, validation, test, split = group_time_split(public, exact)

    experts: dict[str, dict[str, Any]] = {}
    fit_report: dict[str, Any] = {}
    for index, setup in enumerate(EXPERTS):
        train_scope = train[train.setup_type.eq(setup)]
        validation_scope = validation[validation.setup_type.eq(setup)]
        if len(train_scope) < 250 or len(validation_scope) < 20:
            continue
        expert = fit_expert(train_scope, validation_scope, 1717 + index * 17)
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
                float(
                    mean_absolute_error(
                        validation_scope.net_stress_1_5,
                        predicted,
                    )
                ),
                6,
            ),
        }

    validation = predict(validation, experts)
    test = predict(test, experts)
    floors = make_floors(validation)
    selected_validation = event_group_schedule(validation, floors)
    selected_test = event_group_schedule(test, floors)
    validation_metrics = metrics(
        selected_validation,
        "net_stress_1_0",
        "moe_v17_validation",
    )
    test_metrics = metrics(selected_test, "net_stress_1_0", "moe_v17_test")
    stress_metrics = {
        f"{multiplier:.1f}x": {
            "validation": metrics(
                selected_validation,
                "net_stress_1_0"
                if multiplier == 1.0
                else f"net_stress_{str(multiplier).replace('.', '_')}",
                f"v17_validation_cost_{multiplier:.1f}x",
            ),
            "test": metrics(
                selected_test,
                "net_stress_1_0"
                if multiplier == 1.0
                else f"net_stress_{str(multiplier).replace('.', '_')}",
                f"v17_test_cost_{multiplier:.1f}x",
            ),
        }
        for multiplier in STRESS_MULTIPLIERS
    }
    baseline = baseline_metrics(exact)
    test_stress = stress_metrics["1.5x"]["test"]
    release_checks = {
        "event_group_split_isolated": not any(split["group_overlap"].values()),
        "validation_trades": validation_metrics.get("trades", 0) >= 20,
        "validation_net_after_1_5x_cost": stress_metrics["1.5x"]["validation"].get(
            "net_pct_points", 0.0
        )
        > 0,
        "test_trades": test_metrics.get("trades", 0) >= 20,
        "test_net_after_1_5x_cost": test_stress.get("net_pct_points", 0.0) > 0,
        "test_pf_after_1_5x_cost": test_stress.get("profit_factor", 0.0) >= 1.15,
        "test_regimes": test_metrics.get("regimes", 0) >= 2,
        "test_symbols": test_metrics.get("symbols", 0) >= 8,
    }
    eligible = all(release_checks.values())
    generated_at = pd.Timestamp.now(tz="UTC").isoformat()
    report = {
        "experiment": MODEL_VERSION,
        "generated_at": generated_at,
        "scope": "S0 exact V5 lineage; grouped market events; public Binance train-only support",
        "data": {
            "public_rows": int(len(public)),
            "exact_v5_rows_before_dedup": int(len(exact) + deduplicated),
            "exact_v5_rows_after_dedup": int(len(exact)),
            "independent_event_groups": int(exact.event_group_id.nunique()),
            "deduplicated_rows": int(deduplicated),
            "v5_sources": exact.source.value_counts().to_dict(),
            "public_weight": 0.05,
            "event_group_weight_capped": True,
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
        "training_summary": report,
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
            "事件组隔离后的样本外与 1.5 倍成本压力测试通过；仍只进入独立影子，不接管实盘。"
            if eligible
            else "事件组隔离后的样本外证据未达到放行标准，只保留研究影子，不接管实盘。"
        ),
        "data": report["data"],
        "baseline": baseline,
        "validation": validation_metrics,
        "untouched_test": test_metrics,
        "cost_stress": stress_metrics,
        "release_checks": release_checks,
        "active_gates": len(floors),
        "affects_live_admission": False,
        "online_learning": "线上只推理并记录；下一轮训练使用隔离版本和事件组去重后的 V5 实盘与影子数据。",
    }
    args.status.write_text(
        json.dumps(status, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
