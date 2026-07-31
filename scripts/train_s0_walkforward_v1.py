from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import train_s0_conditional_direction_v1 as base  # noqa: E402
from scripts import train_s0_derivatives_v1 as derivatives  # noqa: E402
from scripts import train_s0_orderbook_v1 as orderbook  # noqa: E402

MODEL_VERSION = "s0_walkforward_v1"
DEFAULT_CACHE = ROOT / "data" / "research" / MODEL_VERSION / "prepared.parquet"
DEFAULT_OUTPUT = ROOT / "data" / "research" / MODEL_VERSION
LABEL = "research_net_60m"
FEATURES = (
    *derivatives.DERIVATIVE_NUMERIC,
    *orderbook.BOOK_NUMERIC,
    *base.CATEGORICAL,
)


@dataclass(frozen=True)
class Window:
    name: str
    train_start: pd.Timestamp
    early_stop_start: pd.Timestamp
    calibration_start: pd.Timestamp
    evaluation_start: pd.Timestamp
    evaluation_end: pd.Timestamp | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Walk-forward S0 model with trailing training and calibration windows."
    )
    parser.add_argument("--source", type=Path, default=derivatives.DEFAULT_SOURCE)
    parser.add_argument("--minute-dir", type=Path, default=derivatives.DEFAULT_MINUTE_DIR)
    parser.add_argument(
        "--derivatives-dir",
        type=Path,
        default=derivatives.DEFAULT_DERIVATIVES_DIR,
    )
    parser.add_argument(
        "--book-dir",
        type=Path,
        default=orderbook.DEFAULT_BOOK_DIR,
    )
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-train-rows", type=int, default=500_000)
    return parser.parse_args()


def build_dataset(args: argparse.Namespace) -> pd.DataFrame:
    if args.cache.exists():
        return pd.read_parquet(args.cache)
    source = pd.read_parquet(args.source)
    original_horizons = base.HORIZONS
    original_numeric = base.NUMERIC
    original_features = base.FEATURES
    try:
        base.HORIZONS = derivatives.HORIZONS
        base.NUMERIC = derivatives.BASE_NUMERIC
        base.FEATURES = derivatives.BASE_FEATURES
        relabelled = base.relabel_horizons(source, args.minute_dir)
        prepared = derivatives.BASE_PREPARE(relabelled)
    finally:
        base.HORIZONS = original_horizons
        base.NUMERIC = original_numeric
        base.FEATURES = original_features
    enriched = derivatives.enrich_derivatives(prepared, args.derivatives_dir)
    enriched = orderbook.enrich_orderbook(enriched, args.book_dir)
    keep = [
        "symbol",
        "open_time",
        "time",
        "direction",
        "setup_type",
        "market_regime",
        "rank_percentile",
        LABEL,
        *derivatives.DERIVATIVE_NUMERIC,
        *orderbook.BOOK_NUMERIC,
    ]
    result = enriched[keep].dropna(subset=[LABEL]).sort_values("open_time")
    args.cache.parent.mkdir(parents=True, exist_ok=True)
    result.to_parquet(args.cache, index=False)
    return result


def fold_boundaries(frame: pd.DataFrame) -> list[Window]:
    times = pd.Series(frame.time.drop_duplicates().sort_values().to_numpy())
    origins = [pd.Timestamp(times.quantile(q)) for q in (0.58, 0.66, 0.74, 0.82)]
    windows: list[Window] = []
    for index, origin in enumerate(origins):
        evaluation_end = origins[index + 1] if index + 1 < len(origins) else None
        windows.append(
            Window(
                name="untouched_test" if evaluation_end is None else f"development_{index + 1}",
                train_start=origin - pd.Timedelta(days=77),
                early_stop_start=origin - pd.Timedelta(days=17),
                calibration_start=origin - pd.Timedelta(days=7),
                evaluation_start=origin,
                evaluation_end=evaluation_end,
            )
        )
    return windows


def _fit(
    train: pd.DataFrame,
    early_stop: pd.DataFrame,
    max_rows: int,
    seed: int,
) -> tuple[Any, Any]:
    sampled = base.sample_training(train, max_rows, seed)
    recency = 0.55 + 0.90 * (
        sampled.open_time - sampled.open_time.min()
    ) / max(sampled.open_time.max() - sampled.open_time.min(), 1)
    common = {
        "n_estimators": 700,
        "learning_rate": 0.025,
        "num_leaves": 23,
        "min_child_samples": 700,
        "subsample": 0.82,
        "colsample_bytree": 0.82,
        "reg_alpha": 5.0,
        "reg_lambda": 20.0,
        "n_jobs": -1,
        "verbosity": -1,
    }
    classifier = lgb.LGBMClassifier(
        objective="binary",
        random_state=seed,
        **common,
    )
    center = lgb.LGBMRegressor(
        objective="huber",
        alpha=0.70,
        random_state=seed + 1,
        **common,
    )
    callbacks = [lgb.early_stopping(60, verbose=False)]
    classifier.fit(
        sampled[list(FEATURES)],
        sampled[LABEL].gt(0).astype("int8"),
        sample_weight=recency,
        eval_set=[
            (
                early_stop[list(FEATURES)],
                early_stop[LABEL].gt(0).astype("int8"),
            )
        ],
        eval_metric="binary_logloss",
        callbacks=callbacks,
        categorical_feature=list(base.CATEGORICAL),
    )
    center.fit(
        sampled[list(FEATURES)],
        sampled[LABEL],
        sample_weight=recency,
        eval_set=[(early_stop[list(FEATURES)], early_stop[LABEL])],
        eval_metric="l1",
        callbacks=callbacks,
        categorical_feature=list(base.CATEGORICAL),
    )
    return classifier, center


def _predict(frame: pd.DataFrame, classifier: Any, center: Any) -> pd.DataFrame:
    result = frame.copy()
    matrix = result[list(FEATURES)]
    probability = classifier.predict_proba(matrix)[:, 1]
    center_value = center.predict(matrix)
    result["model_score"] = probability + 0.05 * np.tanh(center_value)
    result["selected_net_pct"] = result[LABEL]
    result["selected_horizon"] = "60m"
    result["hold_minutes"] = 60
    result["batch"] = (result.open_time.astype("int64") // 300_000).astype("int64")
    return result


def select_from_calibration(
    prediction: pd.DataFrame,
) -> tuple[str | None, float | None, dict[str, Any]]:
    candidates: list[tuple[float, float, str, float, dict[str, Any]]] = []
    for lane in base.lanes(prediction):
        scoped = prediction[base.lane_mask(prediction, lane)]
        if len(scoped) < 100:
            continue
        for quantile in (0.94, 0.96, 0.98, 0.99):
            threshold = float(scoped.model_score.quantile(quantile))
            summary = base.metrics(base.schedule(scoped, threshold))
            if (
                summary["trades"] >= 20
                and summary["net_pct_points"] > 0
                and summary["profit_factor"] >= 1.05
            ):
                candidates.append(
                    (
                        summary["conservative_mean_net_pct"],
                        summary["profit_factor"],
                        lane,
                        threshold,
                        summary,
                    )
                )
    if not candidates:
        return None, None, base.metrics(pd.DataFrame())
    _, _, lane, threshold, summary = max(candidates)
    return lane, threshold, summary


def _scope(frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp | None) -> pd.DataFrame:
    mask = frame.time.ge(start)
    if end is not None:
        mask &= frame.time.lt(end)
    return frame[mask]


def train(args: argparse.Namespace) -> dict[str, Any]:
    frame = build_dataset(args)
    for column in base.CATEGORICAL:
        frame[column] = frame[column].astype("category")
    windows = fold_boundaries(frame)
    fold_reports: list[dict[str, Any]] = []
    selected_rows: list[pd.DataFrame] = []
    final_bundle: dict[str, Any] | None = None
    for index, window in enumerate(windows):
        embargo = pd.Timedelta(hours=1)
        train_frame = _scope(
            frame,
            window.train_start,
            window.early_stop_start - embargo,
        )
        early_stop = _scope(
            frame,
            window.early_stop_start,
            window.calibration_start - embargo,
        )
        calibration = _scope(
            frame,
            window.calibration_start,
            window.evaluation_start - embargo,
        )
        evaluation = _scope(
            frame,
            window.evaluation_start,
            window.evaluation_end,
        )
        classifier, center = _fit(
            train_frame,
            early_stop,
            args.max_train_rows,
            1701 + index * 20,
        )
        calibration_prediction = _predict(calibration, classifier, center)
        lane, threshold, calibration_summary = select_from_calibration(
            calibration_prediction
        )
        if lane is None or threshold is None:
            selected = evaluation.iloc[:0].copy()
        else:
            evaluation_prediction = _predict(evaluation, classifier, center)
            selected = base.schedule(
                evaluation_prediction[base.lane_mask(evaluation_prediction, lane)],
                threshold,
            )
        summary = base.metrics(selected)
        fold_reports.append(
            {
                "name": window.name,
                "train_start": window.train_start.isoformat(),
                "early_stop_start": window.early_stop_start.isoformat(),
                "calibration_start": window.calibration_start.isoformat(),
                "evaluation_start": window.evaluation_start.isoformat(),
                "evaluation_end": (
                    window.evaluation_end.isoformat()
                    if window.evaluation_end is not None
                    else None
                ),
                "rows": {
                    "train": int(len(train_frame)),
                    "early_stop": int(len(early_stop)),
                    "calibration": int(len(calibration)),
                    "evaluation": int(len(evaluation)),
                },
                "lane": lane,
                "threshold": threshold,
                "calibration": calibration_summary,
                "evaluation": summary,
            }
        )
        if window.name != "untouched_test":
            selected_rows.append(selected)
        else:
            final_bundle = {
                "version": MODEL_VERSION,
                "classifier": classifier,
                "center": center,
                "features": list(FEATURES),
                "categorical": list(base.CATEGORICAL),
                "lane": lane,
                "threshold": threshold,
                "affects_live_admission": False,
            }
            test_selected = selected
    development = (
        pd.concat(selected_rows, ignore_index=True)
        if selected_rows
        else pd.DataFrame()
    )
    development_summary = base.metrics(development)
    test_summary = base.metrics(test_selected)
    stress = {
        "1.5x": base.metrics(test_selected, extra_cost_pct=0.06),
        "2.0x": base.metrics(test_selected, extra_cost_pct=0.12),
    }
    development_folds = [
        item["evaluation"] for item in fold_reports if item["name"] != "untouched_test"
    ]
    release_checks = {
        "all_development_folds_positive": bool(development_folds)
        and all(item["net_pct_points"] > 0 for item in development_folds),
        "development_pf": development_summary.get("profit_factor", 0.0) >= 1.15,
        "development_net": development_summary.get("net_pct_points", 0.0) > 0,
        "test_trades": test_summary.get("trades", 0) >= 50,
        "test_pf": test_summary.get("profit_factor", 0.0) >= 1.15,
        "test_net": test_summary.get("net_pct_points", 0.0) > 0,
        "stressed_pf": stress["1.5x"].get("profit_factor", 0.0) >= 1.05,
        "stressed_net": stress["1.5x"].get("net_pct_points", 0.0) > 0,
        "symbols": test_summary.get("symbols", 0) >= 10,
    }
    eligible = all(release_checks.values())
    report = {
        "model_version": MODEL_VERSION,
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "scope": "isolated walk-forward S0 research; never affects live automatically",
        "method": (
            "trailing 77d window: 60d fit, 10d early stop, 7d calibration; "
            "adaptive lane and threshold selected without evaluation lookahead"
        ),
        "data": {
            "rows": int(len(frame)),
            "symbols": int(frame.symbol.nunique()),
            "start": frame.time.min().isoformat(),
            "end": frame.time.max().isoformat(),
        },
        "folds": fold_reports,
        "development": development_summary,
        "untouched_test": test_summary,
        "cost_stress": stress,
        "release_checks": release_checks,
        "decision": "shadow_candidate" if eligible else "research_only_not_eligible",
        "affects_live_admission": False,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / f"{MODEL_VERSION}_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if final_bundle is not None:
        joblib.dump(
            final_bundle,
            args.output / f"{MODEL_VERSION}.joblib",
            compress=3,
        )
    return report


def main() -> None:
    report = train(parse_args())
    print(
        json.dumps(
            {
                key: report.get(key)
                for key in (
                    "model_version",
                    "development",
                    "untouched_test",
                    "cost_stress",
                    "release_checks",
                    "decision",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
