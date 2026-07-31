from __future__ import annotations

import argparse
import json
import math
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

from scripts.benchmark_s0_exit_profiles import relabel_profile  # noqa: E402

MODEL_VERSION = "s0_conditional_direction_v1"
DEFAULT_SOURCE = ROOT / "data" / "research" / "s0_public_1m" / "s0_candidates_1m.parquet"
DEFAULT_MINUTE_DIR = ROOT / "data" / "research" / "s0_public_1m" / "parquet"
DEFAULT_OUTPUT = ROOT / "data" / "research" / MODEL_VERSION
BASE_ROUND_TRIP_COST_PCT = 0.12
HORIZONS = {
    "3m": ("research_net_3m", 3),
    "5m": ("research_net_5m", 5),
    "10m": ("research_net_10m", 10),
}
CATEGORICAL = ("direction", "setup_type", "market_regime")
NUMERIC = (
    "rank_percentile",
    "quality",
    "expected_net_pct",
    "lower_expected_net_pct",
    "cost_ratio",
    "atr_pct",
    "ret_1_dir",
    "ret_3_dir",
    "ret_6_dir",
    "ret_12_dir",
    "ret_24_dir",
    "volume_acceleration",
    "directional_flow",
    "path_efficiency",
    "extension_atr",
    "impulse_atr",
    "range_position_dir",
    "quote_volume_1h_log",
    "market_ret_dir",
    "market_breadth_dir",
    "market_atr_pct",
    "cross_section_dispersion",
    "breadth_extremity",
    "market_trend_strength",
    "volume_persistence",
    "directed_flow",
    "medium_path",
    "anti_chase",
    "regime_fit",
    "entry_quality",
    "confirmations",
    "micro_ret_1_dir",
    "micro_ret_3_dir",
    "micro_ret_5_dir",
    "micro_path_efficiency",
    "micro_directional_minutes",
    "micro_realized_vol",
    "micro_max_impulse",
    "micro_adverse_wick",
    "micro_close_location",
    "micro_taker_imbalance",
    "micro_taker_imbalance_last",
    "micro_taker_imbalance_slope",
    "micro_taker_persistence",
    "micro_quote_acceleration",
    "micro_trade_acceleration",
    "micro_volume_concentration",
)
FEATURES = NUMERIC + CATEGORICAL


@dataclass
class HorizonModels:
    conservative: Any
    center: Any
    classifier: Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Train a cost-aware, conditional one-direction S0 research challenger."
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--minute-dir", type=Path, default=DEFAULT_MINUTE_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-train-rows", type=int, default=650_000)
    return parser.parse_args()


def relabel_horizons(frame: pd.DataFrame, minute_dir: Path) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for symbol, scoped in frame.groupby("symbol", sort=True):
        path = minute_dir / f"{symbol}.parquet"
        if not path.exists():
            continue
        minute = pd.read_parquet(
            path,
            columns=["open_time", "open", "high", "low", "close"],
        ).sort_values("open_time")
        built = scoped.copy()
        valid = pd.Series(True, index=built.index)
        for horizon, (label, hold_minutes) in HORIZONS.items():
            result = relabel_profile(
                minute,
                built,
                stop_atr=0.85,
                take_profit_r=1.05,
                hold_minutes=hold_minutes,
            )
            built[label] = result.net_pct
            built[f"research_outcome_{horizon}"] = result.outcome
            valid &= result.valid
        parts.append(built[valid].copy())
    if not parts:
        return frame.iloc[:0].copy()
    return pd.concat(parts, ignore_index=True).sort_values("open_time")


def prepare(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame[frame.minute_feature_valid.fillna(False)].copy()
    result["time"] = pd.to_datetime(result.open_time, unit="ms", utc=True)
    sign = np.where(result.direction.eq("LONG"), 1.0, -1.0)
    for bars in (1, 3, 6, 12, 24):
        result[f"ret_{bars}_dir"] = pd.to_numeric(
            result[f"ret_{bars}"], errors="coerce"
        ).fillna(0.0) * sign
    result["range_position_dir"] = np.where(
        result.direction.eq("LONG"),
        result.range_position,
        1.0 - result.range_position,
    )
    result["market_ret_dir"] = result.market_ret_12 * sign
    result["market_breadth_dir"] = np.where(
        result.direction.eq("LONG"),
        result.market_breadth,
        1.0 - result.market_breadth,
    )
    base = result.drop_duplicates(["open_time", "symbol"])[
        ["open_time", "ret_12"]
    ].copy()
    dispersion = base.groupby("open_time", sort=False).ret_12.std().fillna(0.0)
    result["cross_section_dispersion"] = result.open_time.map(dispersion).fillna(0.0)
    result["breadth_extremity"] = (result.market_breadth - 0.5).abs()
    result["market_trend_strength"] = (
        result.market_ret_12.abs()
        / result.market_atr_pct.abs().clip(lower=0.01)
    ).clip(upper=10.0)
    categories = {
        "direction": ["LONG", "SHORT"],
        "setup_type": ["pullback", "momentum", "prebreakout", "breakout"],
        "market_regime": ["quiet", "rotation", "broad_up", "broad_down", "panic"],
    }
    for column, values in categories.items():
        result[column] = pd.Categorical(result[column], categories=values)
    for column in NUMERIC:
        result[column] = pd.to_numeric(result[column], errors="coerce").fillna(0.0)
    for label, _ in HORIZONS.values():
        result[label] = pd.to_numeric(result[label], errors="coerce")
    return result.dropna(subset=[label for label, _ in HORIZONS.values()]).sort_values(
        "open_time"
    )


def split_times(frame: pd.DataFrame) -> dict[str, pd.Timestamp]:
    unique = pd.Series(frame.time.drop_duplicates().sort_values().to_numpy())
    return {
        "early_stop": pd.Timestamp(unique.quantile(0.43)),
        "calibration": pd.Timestamp(unique.quantile(0.50)),
        "validation": pd.Timestamp(unique.quantile(0.67)),
        "test": pd.Timestamp(unique.quantile(0.82)),
    }


def sample_training(frame: pd.DataFrame, limit: int, seed: int) -> pd.DataFrame:
    if len(frame) <= limit:
        return frame
    recent = frame[frame.time >= frame.time.quantile(0.65)]
    recent_limit = min(len(recent), int(limit * 0.45))
    older = frame.drop(index=recent.index)
    old_limit = limit - recent_limit
    return pd.concat(
        [
            older.sample(old_limit, random_state=seed),
            recent.sample(recent_limit, random_state=seed + 1),
        ]
    ).sort_values("open_time")


def fit_horizon(
    train: pd.DataFrame,
    early_stop: pd.DataFrame,
    label: str,
    seed: int,
) -> HorizonModels:
    recency = 0.65 + 0.70 * (
        train.open_time - train.open_time.min()
    ) / max(train.open_time.max() - train.open_time.min(), 1)
    common = {
        "n_estimators": 800,
        "learning_rate": 0.025,
        "num_leaves": 23,
        "min_child_samples": 800,
        "subsample": 0.82,
        "colsample_bytree": 0.78,
        "reg_alpha": 4.0,
        "reg_lambda": 18.0,
        "n_jobs": -1,
        "verbosity": -1,
    }
    conservative = lgb.LGBMRegressor(
        objective="quantile",
        alpha=0.25,
        random_state=seed,
        **common,
    )
    center = lgb.LGBMRegressor(
        objective="huber",
        alpha=0.70,
        random_state=seed + 1,
        **common,
    )
    classifier = lgb.LGBMClassifier(
        objective="binary",
        random_state=seed + 2,
        **common,
    )
    callbacks = [lgb.early_stopping(60, verbose=False)]
    matrix = train[list(FEATURES)]
    valid_matrix = early_stop[list(FEATURES)]
    conservative.fit(
        matrix,
        train[label],
        sample_weight=recency,
        eval_set=[(valid_matrix, early_stop[label])],
        eval_metric="quantile",
        callbacks=callbacks,
        categorical_feature=list(CATEGORICAL),
    )
    center.fit(
        matrix,
        train[label],
        sample_weight=recency,
        eval_set=[(valid_matrix, early_stop[label])],
        eval_metric="l1",
        callbacks=callbacks,
        categorical_feature=list(CATEGORICAL),
    )
    classifier.fit(
        matrix,
        train[label].gt(0).astype("int8"),
        sample_weight=recency,
        eval_set=[(valid_matrix, early_stop[label].gt(0).astype("int8"))],
        eval_metric="binary_logloss",
        callbacks=callbacks,
        categorical_feature=list(CATEGORICAL),
    )
    return HorizonModels(
        conservative=conservative,
        center=center,
        classifier=classifier,
    )


def predict(frame: pd.DataFrame, models: dict[str, HorizonModels]) -> pd.DataFrame:
    result = frame.copy()
    matrix = result[list(FEATURES)]
    scores: list[np.ndarray] = []
    for horizon, fitted in models.items():
        conservative = (
            fitted["conservative"] if isinstance(fitted, dict) else fitted.conservative
        )
        center_model = fitted["center"] if isinstance(fitted, dict) else fitted.center
        classifier = (
            fitted["classifier"] if isinstance(fitted, dict) else fitted.classifier
        )
        q25 = conservative.predict(matrix)
        center = center_model.predict(matrix)
        probability = classifier.predict_proba(matrix)[:, 1]
        result[f"{horizon}_q25"] = q25
        result[f"{horizon}_center"] = center
        result[f"{horizon}_probability"] = probability
        # The natural-distribution win probability is comparable across
        # horizons. Absolute regressions are retained as diagnostics and a
        # small tie-breaker, not treated as calibrated probabilities.
        score = probability + 0.05 * np.tanh(center)
        result[f"{horizon}_score"] = score
        scores.append(score)
    matrix_scores = np.column_stack(scores)
    names = np.asarray(list(models))
    best = matrix_scores.argmax(axis=1)
    result["selected_horizon"] = names[best]
    result["model_score"] = matrix_scores[np.arange(len(result)), best]
    label_matrix = np.column_stack(
        [result[HORIZONS[horizon][0]].to_numpy(dtype=float) for horizon in models]
    )
    result["selected_net_pct"] = label_matrix[np.arange(len(result)), best]
    result["hold_minutes"] = result.selected_horizon.map(
        {name: minutes for name, (_, minutes) in HORIZONS.items()}
    )
    result["batch"] = (result.open_time.astype("int64") // 300_000).astype("int64")
    return result


def lane_mask(frame: pd.DataFrame, lane: str) -> pd.Series:
    if lane == "global":
        return pd.Series(True, index=frame.index)
    direction, dimension, value = lane.split("|", 2)
    mask = frame.direction.astype(str).eq(direction)
    if dimension == "regime":
        mask &= frame.market_regime.astype(str).eq(value)
    elif dimension == "setup":
        mask &= frame.setup_type.astype(str).eq(value)
    return mask


def lanes(frame: pd.DataFrame) -> list[str]:
    values = ["global", "LONG|all|all", "SHORT|all|all"]
    for direction in ("LONG", "SHORT"):
        for regime in frame.market_regime.dropna().astype(str).unique():
            values.append(f"{direction}|regime|{regime}")
        for setup in frame.setup_type.dropna().astype(str).unique():
            values.append(f"{direction}|setup|{setup}")
    return values


def schedule(frame: pd.DataFrame, threshold: float) -> pd.DataFrame:
    eligible = frame[frame.model_score.ge(threshold)].copy()
    if eligible.empty:
        return eligible
    ranked = (
        eligible.sort_values(
            ["batch", "model_score", "rank_percentile"],
            ascending=[True, False, False],
        )
        .drop_duplicates("batch")
        .sort_values("open_time")
    )
    selected: list[int] = []
    free_at = -1
    symbol_free: dict[tuple[str, str], int] = {}
    for row in ranked.itertuples():
        timestamp = int(row.open_time)
        key = (str(row.symbol), str(row.direction))
        if timestamp < free_at or timestamp < symbol_free.get(key, -1):
            continue
        selected.append(row.Index)
        free_at = timestamp + int(row.hold_minutes) * 60_000
        symbol_free[key] = timestamp + 30 * 60_000
    return ranked.loc[selected].sort_values("open_time")


def metrics(frame: pd.DataFrame, *, extra_cost_pct: float = 0.0) -> dict[str, Any]:
    if frame.empty:
        return {"trades": 0, "net_pct_points": 0.0, "profit_factor": 0.0}
    values = frame.selected_net_pct.astype(float) - extra_cost_pct
    gains = float(values[values > 0].sum())
    losses = float(-values[values < 0].sum())
    curve = values.cumsum()
    drawdown = curve.cummax() - curve
    standard_error = float(values.std(ddof=1) / math.sqrt(len(values))) if len(values) > 1 else 0.0
    return {
        "trades": int(len(values)),
        "symbols": int(frame.symbol.nunique()),
        "regimes": int(frame.market_regime.nunique()),
        "win_rate": round(float(values.gt(0).mean() * 100.0), 4),
        "net_pct_points": round(float(values.sum()), 6),
        "mean_net_pct": round(float(values.mean()), 6),
        "conservative_mean_net_pct": round(float(values.mean() - standard_error), 6),
        "profit_factor": round(gains / losses, 4) if losses else (999.0 if gains else 0.0),
        "max_drawdown_pct_points": round(float(drawdown.max()), 6),
        "horizons": frame.selected_horizon.value_counts().to_dict(),
    }


def calibration_folds(frame: pd.DataFrame) -> list[pd.DataFrame]:
    ordered = frame.sort_values("time")
    boundaries = ordered.time.quantile([0.0, 1 / 3, 2 / 3, 1.0]).tolist()
    return [
        ordered[
            ordered.time.ge(boundaries[index])
            & (
                ordered.time.lt(boundaries[index + 1])
                if index < 2
                else ordered.time.le(boundaries[index + 1])
            )
        ]
        for index in range(3)
    ]


def calibrate_lane(frame: pd.DataFrame, lane: str) -> dict[str, Any]:
    scoped = frame[lane_mask(frame, lane)].copy()
    if len(scoped) < 300:
        return {"active": False, "reason": "insufficient_rows"}
    folds = calibration_folds(scoped)
    choices: list[tuple[float, float, float, dict[str, Any]]] = []
    for quantile in (0.90, 0.93, 0.95, 0.965, 0.975, 0.985, 0.99, 0.995):
        threshold = float(scoped.model_score.quantile(quantile))
        fold_metrics = [metrics(schedule(fold, threshold)) for fold in folds]
        if any(item["trades"] < 20 for item in fold_metrics):
            continue
        combined = metrics(schedule(scoped, threshold))
        positive = sum(
            item["net_pct_points"] > 0 and item["profit_factor"] > 1.0
            for item in fold_metrics
        )
        worst_pf = min(item["profit_factor"] for item in fold_metrics)
        objective = (
            positive * 10.0
            + worst_pf
            + combined["conservative_mean_net_pct"]
        )
        choices.append(
            (
                objective,
                threshold,
                quantile,
                {"combined": combined, "folds": fold_metrics},
            )
        )
    if not choices:
        return {"active": False, "reason": "insufficient_scheduled_samples"}
    _, threshold, quantile, detail = max(choices, key=lambda item: item[0])
    active = bool(
        all(
            item["net_pct_points"] > 0 and item["profit_factor"] > 1.0
            for item in detail["folds"]
        )
        and detail["combined"]["trades"] >= 75
        and detail["combined"]["profit_factor"] >= 1.12
        and detail["combined"]["conservative_mean_net_pct"] > 0
    )
    return {
        "active": active,
        "reason": "three_fold_positive" if active else "calibration_not_stable",
        "threshold": threshold,
        "quantile": quantile,
        **detail,
    }


def train(args: argparse.Namespace) -> dict[str, Any]:
    source = pd.read_parquet(args.source)
    relabelled = relabel_horizons(source, args.minute_dir)
    frame = prepare(relabelled)
    cuts = split_times(frame)
    embargo = pd.Timedelta(hours=1)
    train_pool = frame[frame.time < cuts["early_stop"] - embargo]
    early_stop = frame[
        frame.time.ge(cuts["early_stop"])
        & frame.time.lt(cuts["calibration"] - embargo)
    ]
    calibration = frame[
        frame.time.ge(cuts["calibration"])
        & frame.time.lt(cuts["validation"] - embargo)
    ]
    validation = frame[
        frame.time.ge(cuts["validation"])
        & frame.time.lt(cuts["test"] - embargo)
    ]
    test = frame[frame.time.ge(cuts["test"])]
    train_sample = sample_training(train_pool, args.max_train_rows, 731)

    fitted: dict[str, HorizonModels] = {}
    fit_report: dict[str, Any] = {}
    for index, (horizon, (label, _)) in enumerate(HORIZONS.items()):
        models = fit_horizon(train_sample, early_stop, label, 731 + index * 20)
        fitted[horizon] = models
        fit_report[horizon] = {
            "label": label,
            "conservative_iterations": models.conservative.best_iteration_,
            "center_iterations": models.center.best_iteration_,
            "classifier_iterations": models.classifier.best_iteration_,
        }

    calibration_pred = predict(calibration, fitted)
    validation_pred = predict(validation, fitted)
    test_pred = predict(test, fitted)
    calibrated = {
        lane: calibrate_lane(calibration_pred, lane)
        for lane in lanes(calibration_pred)
    }
    active = {
        lane: detail for lane, detail in calibrated.items() if detail.get("active")
    }
    validation_active_lanes: dict[str, dict[str, Any]] = {}
    validation_candidates: list[tuple[float, str, pd.DataFrame, dict[str, Any]]] = []
    for lane, detail in active.items():
        selected = schedule(
            validation_pred[lane_mask(validation_pred, lane)],
            float(detail["threshold"]),
        )
        summary = metrics(selected)
        validation_active_lanes[lane] = summary
        if (
            summary["trades"] >= 40
            and summary["net_pct_points"] > 0
            and summary["profit_factor"] > 1.05
            and summary["conservative_mean_net_pct"] > 0
        ):
            validation_candidates.append(
                (summary["conservative_mean_net_pct"], lane, selected, summary)
            )

    selected_lane: str | None = None
    selected_threshold: float | None = None
    validation_summary = metrics(pd.DataFrame())
    test_summary = metrics(pd.DataFrame())
    stress = {"1.5x": metrics(pd.DataFrame()), "2.0x": metrics(pd.DataFrame())}
    if validation_candidates:
        _, selected_lane, _, validation_summary = max(
            validation_candidates, key=lambda item: item[0]
        )
        selected_threshold = float(active[selected_lane]["threshold"])
        selected_test = schedule(
            test_pred[lane_mask(test_pred, selected_lane)],
            selected_threshold,
        )
        test_summary = metrics(selected_test)
        stress = {
            "1.5x": metrics(selected_test, extra_cost_pct=0.06),
            "2.0x": metrics(selected_test, extra_cost_pct=0.12),
        }

    release_checks = {
        "calibration_lane": selected_lane is not None,
        "validation_trades": validation_summary.get("trades", 0) >= 50,
        "validation_pf": validation_summary.get("profit_factor", 0.0) >= 1.15,
        "validation_net": validation_summary.get("net_pct_points", 0.0) > 0,
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
        "scope": "isolated S0 research challenger; never affects live admission automatically",
        "method": (
            "natural-distribution LightGBM horizon experts; conservative quantile, "
            "center estimate and win probability; calibration selects a conditional direction lane"
        ),
        "cost": {
            "base_round_trip_pct": BASE_ROUND_TRIP_COST_PCT,
            "stress_extra_pct": {"1.5x": 0.06, "2.0x": 0.12},
        },
        "data": {
            "source_rows": int(len(source)),
            "exact_path_rows": int(len(relabelled)),
            "prepared_rows": int(len(frame)),
            "train_pool": int(len(train_pool)),
            "train_sample": int(len(train_sample)),
            "early_stop": int(len(early_stop)),
            "calibration": int(len(calibration)),
            "validation": int(len(validation)),
            "test": int(len(test)),
            "symbols": int(frame.symbol.nunique()),
            "cuts": {name: value.isoformat() for name, value in cuts.items()},
        },
        "fit": fit_report,
        "calibrated_lanes": calibrated,
        "active_calibration_lanes": sorted(active),
        "validation_active_lanes": validation_active_lanes,
        "selected_lane": selected_lane,
        "selected_threshold": selected_threshold,
        "validation": validation_summary,
        "untouched_test": test_summary,
        "cost_stress": stress,
        "release_checks": release_checks,
        "decision": "shadow_candidate" if eligible else "research_only_not_eligible",
        "affects_live_admission": False,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    bundle = {
        "version": MODEL_VERSION,
        "models": {
            horizon: {
                "conservative": models.conservative,
                "center": models.center,
                "classifier": models.classifier,
            }
            for horizon, models in fitted.items()
        },
        "features": list(FEATURES),
        "categorical": list(CATEGORICAL),
        "horizons": HORIZONS,
        "selected_lane": selected_lane,
        "selected_threshold": selected_threshold,
        "decision": report["decision"],
        "affects_live_admission": False,
    }
    joblib.dump(bundle, args.output / f"{MODEL_VERSION}.joblib", compress=3)
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
