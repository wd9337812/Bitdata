from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.train_s0_hybrid_moe import (  # noqa: E402
    CATEGORICAL,
    EXPERTS,
    FEATURES,
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
from scripts.train_s0_moe_v1_7 import (  # noqa: E402
    attach_event_groups,
    cap_event_group_weights,
    event_group_schedule,
)


MODEL_VERSION = "s0_binance_moe_v1_8"
PUBLIC_SOURCE = ROOT / "data" / "research" / "s0_public_1m" / "s0_candidates_1m.parquet"
VPS_HISTORY = ROOT / "data" / "research" / "vps_history_20260728_v5"
OUTPUT = ROOT / "data" / "research" / MODEL_VERSION
PUBLIC_ROUND_TRIP_COST_PCT = 0.12


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Train a walk-forward, event-isolated S0 MoE research challenger."
    )
    parser.add_argument("--public-source", type=Path, default=PUBLIC_SOURCE)
    parser.add_argument("--vps-history", type=Path, default=VPS_HISTORY)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--max-public-per-expert", type=int, default=250_000)
    parser.add_argument("--folds", type=int, default=3)
    return parser.parse_args()


def prepare_public(frame: pd.DataFrame) -> pd.DataFrame:
    """Attach explicit costs and cap repeated rows from the same local market wave."""
    output = frame.copy()
    output["gross_pct"] = output.net_pct + PUBLIC_ROUND_TRIP_COST_PCT
    output["cost_pct"] = PUBLIC_ROUND_TRIP_COST_PCT
    for multiplier in STRESS_MULTIPLIERS:
        column = f"net_stress_{str(multiplier).replace('.', '_')}"
        output[column] = output.gross_pct - output.cost_pct * multiplier
    batch = output.time.dt.floor("30min").astype(str)
    output["event_group_id"] = (
        "public:"
        + output.symbol.astype(str)
        + ":"
        + output.direction.astype(str)
        + ":"
        + batch
    )
    output["base_weight"] = 0.02
    group_weight = output.groupby("event_group_id").base_weight.transform("sum").clip(lower=0.02)
    output["base_weight"] *= np.minimum(1.0, 0.08 / group_weight)
    return output


def rolling_group_splits(
    public: pd.DataFrame,
    exact: pd.DataFrame,
    folds: int = 3,
) -> list[tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]]:
    groups = (
        exact.groupby("event_group_id", as_index=False)
        .agg(first_time=("time", "min"))
        .sort_values("first_time")
        .reset_index(drop=True)
    )
    if len(groups) < 60:
        raise RuntimeError("independent V5 event groups are too few for walk-forward validation")
    folds = max(1, min(int(folds), 3))
    validation_share = 0.15
    test_share = 0.15
    first_train_share = 1.0 - folds * test_share - validation_share
    if first_train_share < 0.35:
        raise RuntimeError("walk-forward split leaves too little training history")

    results = []
    total = len(groups)
    validation_count = max(10, int(total * validation_share))
    test_count = max(10, int(total * test_share))
    for index in range(folds):
        train_end = int(total * first_train_share) + index * test_count
        validation_end = train_end + validation_count
        test_end = min(total, validation_end + test_count)
        train_groups = set(groups.iloc[:train_end].event_group_id)
        validation_groups = set(groups.iloc[train_end:validation_end].event_group_id)
        test_groups = set(groups.iloc[validation_end:test_end].event_group_id)
        if not validation_groups or not test_groups:
            continue
        train = pd.concat(
            [public, exact[exact.event_group_id.isin(train_groups)]],
            ignore_index=True,
            sort=False,
        )
        validation = exact[exact.event_group_id.isin(validation_groups)].copy()
        test = exact[exact.event_group_id.isin(test_groups)].copy()
        results.append(
            (
                train,
                validation,
                test,
                {
                    "fold": index + 1,
                    "train_groups": len(train_groups),
                    "validation_groups": len(validation_groups),
                    "test_groups": len(test_groups),
                    "train_end": groups.iloc[train_end - 1].first_time.isoformat(),
                    "validation_end": groups.iloc[validation_end - 1].first_time.isoformat(),
                    "test_end": groups.iloc[test_end - 1].first_time.isoformat(),
                    "overlap": {
                        "train_validation": len(train_groups & validation_groups),
                        "train_test": len(train_groups & test_groups),
                        "validation_test": len(validation_groups & test_groups),
                    },
                },
            )
        )
    return results


def _fit_fold(
    train: pd.DataFrame,
    validation: pd.DataFrame,
    test: pd.DataFrame,
    seed: int,
) -> tuple[dict[str, Any], dict[str, Any], pd.DataFrame, pd.DataFrame]:
    experts: dict[str, dict[str, Any]] = {}
    fit_rows: dict[str, dict[str, int]] = {}
    for index, setup in enumerate(EXPERTS):
        train_scope = train[train.setup_type.eq(setup)]
        validation_scope = validation[validation.setup_type.eq(setup)]
        if len(train_scope) < 250 or len(validation_scope) < 20:
            continue
        experts[setup] = fit_expert(train_scope, validation_scope, seed + index * 17)
        fit_rows[setup] = {
            "train": int(len(train_scope)),
            "validation": int(len(validation_scope)),
        }

    predicted_validation = predict(validation, experts)
    predicted_test = predict(test, experts)
    floors = make_floors(predicted_validation)
    selected_validation = event_group_schedule(predicted_validation, floors)
    selected_test = event_group_schedule(predicted_test, floors)
    fold_report = {
        "experts": fit_rows,
        "gates": len(floors),
        "validation": metrics(
            selected_validation,
            "net_stress_1_0",
            "validation",
        ),
        "test": metrics(selected_test, "net_stress_1_0", "test"),
        "cost_stress": {
            f"{multiplier:.1f}x": {
                "validation": metrics(
                    selected_validation,
                    f"net_stress_{str(multiplier).replace('.', '_')}",
                    f"validation_cost_{multiplier:.1f}x",
                ),
                "test": metrics(
                    selected_test,
                    f"net_stress_{str(multiplier).replace('.', '_')}",
                    f"test_cost_{multiplier:.1f}x",
                ),
            }
            for multiplier in STRESS_MULTIPLIERS
        },
    }
    bundle = {
        "version": MODEL_VERSION,
        "experts": experts,
        "gate_floors": floors,
        "decision": "research_shadow_only",
        "affects_live_admission": False,
    }
    return bundle, fold_report, selected_validation, selected_test


def _combined_metrics(frames: list[pd.DataFrame], column: str, name: str) -> dict[str, Any]:
    usable = [frame for frame in frames if not frame.empty]
    if not usable:
        return {"name": name, "trades": 0, "net_pct_points": 0.0}
    return metrics(pd.concat(usable, ignore_index=True), column, name)


def train(args: argparse.Namespace) -> dict[str, Any]:
    args.output.mkdir(parents=True, exist_ok=True)
    public = prepare_public(load_public(args.public_source, args.max_public_per_expert))
    exact, deduplicated = load_exact_v5(args.vps_history)
    exact = cap_event_group_weights(attach_event_groups(exact, args.vps_history))
    splits = rolling_group_splits(public, exact, args.folds)

    reports = []
    validation_selected: list[pd.DataFrame] = []
    test_selected: list[pd.DataFrame] = []
    final_bundle: dict[str, Any] = {}
    for index, (train_frame, validation, test, split_report) in enumerate(splits):
        bundle, fold_report, selected_validation, selected_test = _fit_fold(
            train_frame,
            validation,
            test,
            seed=1818 + index * 101,
        )
        fold_report["split"] = split_report
        reports.append(fold_report)
        validation_selected.append(selected_validation)
        test_selected.append(selected_test)
        final_bundle = bundle

    aggregate_stress = {
        f"{multiplier:.1f}x": {
            "validation": _combined_metrics(
                validation_selected,
                f"net_stress_{str(multiplier).replace('.', '_')}",
                f"walk_forward_validation_{multiplier:.1f}x",
            ),
            "test": _combined_metrics(
                test_selected,
                f"net_stress_{str(multiplier).replace('.', '_')}",
                f"walk_forward_test_{multiplier:.1f}x",
            ),
        }
        for multiplier in STRESS_MULTIPLIERS
    }
    aggregate_test = aggregate_stress["1.0x"]["test"]
    stressed_test = aggregate_stress["1.5x"]["test"]
    positive_test_folds = sum(
        fold["cost_stress"]["1.5x"]["test"].get("net_pct_points", 0.0) > 0
        for fold in reports
    )
    release_checks = {
        "all_splits_isolated": all(
            not any(fold["split"]["overlap"].values()) for fold in reports
        ),
        "walk_forward_folds": len(reports) >= 3,
        "test_trades": stressed_test.get("trades", 0) >= 60,
        "test_symbols": stressed_test.get("symbols", 0) >= 12,
        "test_regimes": stressed_test.get("regimes", 0) >= 2,
        "positive_test_folds_after_1_5x_cost": positive_test_folds >= 2,
        "test_net_after_1_5x_cost": stressed_test.get("net_pct_points", 0.0) > 0,
        "test_pf_after_1_5x_cost": stressed_test.get("profit_factor", 0.0) >= 1.15,
    }
    eligible = all(release_checks.values())
    generated_at = pd.Timestamp.now(tz="UTC").isoformat()
    report = {
        "experiment": MODEL_VERSION,
        "generated_at": generated_at,
        "scope": (
            "Binance 1m candidates with explicit 0.12% public round-trip cost; "
            "exact VPS V5 events use capped high-weight correction"
        ),
        "data": {
            "public_source_rows": 2_143_799,
            "public_training_rows": int(len(public)),
            "public_symbols": int(public.symbol.nunique()),
            "public_event_groups": int(public.event_group_id.nunique()),
            "public_round_trip_cost_pct": PUBLIC_ROUND_TRIP_COST_PCT,
            "exact_rows_after_dedup": int(len(exact)),
            "exact_event_groups": int(exact.event_group_id.nunique()),
            "deduplicated_exact_rows": int(deduplicated),
            "raw_minute_rows_available": 34_192_395,
        },
        "baseline": baseline_metrics(exact),
        "folds": reports,
        "walk_forward": {
            "test": aggregate_test,
            "cost_stress": aggregate_stress,
            "positive_test_folds_after_1_5x_cost": positive_test_folds,
        },
        "release_checks": release_checks,
        "decision": "shadow_candidate" if eligible else "research_shadow_only",
        "affects_live_admission": False,
    }
    final_bundle["training_summary"] = report
    final_bundle["decision"] = report["decision"]
    final_bundle["affects_live_admission"] = False
    final_bundle["setup_aliases"] = {name: name for name in EXPERTS}
    final_bundle["features"] = list(FEATURES)
    final_bundle["categorical"] = list(CATEGORICAL)
    joblib.dump(
        final_bundle,
        args.output / f"{MODEL_VERSION}.joblib",
        compress=3,
    )
    (args.output / f"{MODEL_VERSION}_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


def main() -> None:
    report = train(parse_args())
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
