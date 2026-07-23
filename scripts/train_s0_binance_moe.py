from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import log_loss, mean_absolute_error, roc_auc_score


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "data" / "research" / "s0_public_1m" / "s0_candidates_1m.parquet"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_moe_v1"
TRAIN_END = pd.Timestamp("2026-05-01", tz="UTC")
VALID_END = pd.Timestamp("2026-06-16", tz="UTC")
EMBARGO = pd.Timedelta(minutes=30)
ROUND_TRIP_COST_PCT = 0.12
EXPERTS = ("momentum", "prebreakout", "pullback")
REGIMES = ("quiet", "rotation", "broad_up", "broad_down", "panic")
CATEGORICAL = ("direction", "market_regime")
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
class Expert:
    classifier: Any
    regressor: Any
    edge_floor: float = 0.0


def prepare(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame[frame.minute_feature_valid].copy()
    result["time"] = pd.to_datetime(result.open_time, unit="ms", utc=True)
    result["net_pct"] = result.net_pct_1m.astype(float)
    result["win"] = result.win_1m.astype(int)
    sign = np.where(result.direction.eq("LONG"), 1.0, -1.0)
    for bars in (1, 3, 6, 12, 24):
        result[f"ret_{bars}_dir"] = result[f"ret_{bars}"] * sign
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
    categories = {
        "direction": ["LONG", "SHORT"],
        "market_regime": ["quiet", "rotation", "broad_up", "broad_down", "panic"],
    }
    for column, values in categories.items():
        result[column] = pd.Categorical(result[column], categories=values)
    return result


def balanced_sample(frame: pd.DataFrame, limit: int, seed: int) -> pd.DataFrame:
    if len(frame) <= limit:
        return frame
    positive = frame[frame.win == 1]
    negative = frame[frame.win == 0]
    positive_count = min(len(positive), limit // 2)
    negative_count = limit - positive_count
    return pd.concat(
        [
            positive.sample(positive_count, random_state=seed),
            negative.sample(negative_count, random_state=seed),
        ]
    ).sort_values("open_time")


def fit_expert(train: pd.DataFrame, valid: pd.DataFrame, seed: int) -> Expert:
    recency = 0.70 + 0.60 * (
        train.open_time - train.open_time.min()
    ) / max(train.open_time.max() - train.open_time.min(), 1)
    classifier = lgb.LGBMClassifier(
        objective="binary",
        n_estimators=1_000,
        learning_rate=0.025,
        num_leaves=19,
        min_child_samples=500,
        subsample=0.82,
        colsample_bytree=0.80,
        reg_alpha=3.0,
        reg_lambda=15.0,
        random_state=seed,
        n_jobs=-1,
        verbosity=-1,
    )
    regressor = lgb.LGBMRegressor(
        objective="huber",
        alpha=0.70,
        n_estimators=1_000,
        learning_rate=0.025,
        num_leaves=19,
        min_child_samples=500,
        subsample=0.82,
        colsample_bytree=0.80,
        reg_alpha=3.0,
        reg_lambda=16.0,
        random_state=seed + 1,
        n_jobs=-1,
        verbosity=-1,
    )
    classifier.fit(
        train[list(FEATURES)],
        train.win,
        sample_weight=recency,
        eval_set=[(valid[list(FEATURES)], valid.win)],
        eval_metric="binary_logloss",
        callbacks=[lgb.early_stopping(80, verbose=False)],
        categorical_feature=list(CATEGORICAL),
    )
    regressor.fit(
        train[list(FEATURES)],
        train.net_pct,
        sample_weight=recency,
        eval_set=[(valid[list(FEATURES)], valid.net_pct)],
        eval_metric="l1",
        callbacks=[lgb.early_stopping(80, verbose=False)],
        categorical_feature=list(CATEGORICAL),
    )
    return Expert(classifier=classifier, regressor=regressor)


def predict(frame: pd.DataFrame, experts: dict[str, Expert]) -> pd.DataFrame:
    result = frame.copy()
    result["pred_win"] = np.nan
    result["pred_net"] = np.nan
    result["model_edge"] = np.nan
    for name, expert in experts.items():
        index = result.setup_type.eq(name)
        matrix = result.loc[index, list(FEATURES)]
        probability = expert.classifier.predict_proba(matrix)[:, 1]
        predicted_net = expert.regressor.predict(matrix)
        result.loc[index, "pred_win"] = probability
        result.loc[index, "pred_net"] = predicted_net
        result.loc[index, "model_edge"] = predicted_net + (probability - 0.5) * 0.50
    return result


def gate_key(setup_type: str, market_regime: str) -> str:
    return f"{setup_type}|{market_regime}"


def schedule(frame: pd.DataFrame, floors: dict[str, float], *, baseline: bool = False) -> pd.DataFrame:
    if baseline:
        eligible = frame[frame.baseline_passed].copy()
        eligible["selection_score"] = eligible.lower_expected_net_pct
    else:
        floor = pd.Series(
            [
                floors.get(gate_key(str(setup), str(regime)), 999.0)
                for setup, regime in zip(frame.setup_type, frame.market_regime)
            ],
            index=frame.index,
        )
        eligible = frame[frame.model_edge >= floor].copy()
        eligible["selection_score"] = eligible.model_edge
    if eligible.empty:
        return eligible
    eligible = eligible.sort_values(
        ["open_time", "selection_score", "rank_percentile"],
        ascending=[True, False, False],
    ).drop_duplicates("open_time", keep="first")
    selected: list[int] = []
    free_at = -1
    last_episode: dict[tuple[str, str], int] = {}
    for row in eligible.itertuples():
        timestamp = int(row.open_time)
        if timestamp < free_at or timestamp < last_episode.get((row.symbol, row.direction), -1):
            continue
        selected.append(row.Index)
        free_at = timestamp + 10 * 60 * 1_000
        last_episode[(row.symbol, row.direction)] = timestamp + 30 * 60 * 1_000
    return frame.loc[selected].sort_values("open_time")


def metrics(frame: pd.DataFrame, name: str) -> dict[str, Any]:
    if frame.empty:
        return {"name": name, "trades": 0}
    values = frame.net_pct.astype(float)
    gains = float(values[values > 0].sum())
    losses = float(-values[values < 0].sum())
    curve = values.cumsum()
    drawdown = curve.cummax() - curve
    days = max((frame.time.max() - frame.time.min()).total_seconds() / 86_400, 1.0)
    return {
        "name": name,
        "trades": len(frame),
        "symbols": int(frame.symbol.nunique()),
        "win_rate": round(float((values > 0).mean() * 100), 4),
        "net_pct_points": round(float(values.sum()), 6),
        "mean_net_pct": round(float(values.mean()), 6),
        "profit_factor": round(gains / losses, 4) if losses else 999.0,
        "max_drawdown_pct_points": round(float(drawdown.max()), 6),
        "trades_per_day": round(len(frame) / days, 4),
        "cost_pct_points": round(len(frame) * ROUND_TRIP_COST_PCT, 6),
    }


def choose_gate_floor(
    frame: pd.DataFrame,
    setup: str,
    regime: str,
) -> tuple[float | None, dict[str, Any]]:
    scoped = frame[frame.setup_type.eq(setup) & frame.market_regime.eq(regime)]
    windows = (
        (pd.Timestamp("2026-05-01", tz="UTC"), pd.Timestamp("2026-05-16", tz="UTC")),
        (pd.Timestamp("2026-05-16", tz="UTC"), pd.Timestamp("2026-06-01", tz="UTC")),
        (pd.Timestamp("2026-06-01", tz="UTC"), VALID_END),
    )
    choices = []
    for quantile in (0.85, 0.90, 0.925, 0.95, 0.965, 0.975, 0.985, 0.99, 0.995):
        floor = float(scoped.model_edge.quantile(quantile))
        floors = {gate_key(setup, regime): floor}
        slices = [
            metrics(schedule(frame[(frame.time >= start) & (frame.time < end)], floors), f"fold_{index}")
            for index, (start, end) in enumerate(windows, start=1)
        ]
        if any(item.get("trades", 0) < 12 for item in slices):
            continue
        combined = metrics(schedule(frame, floors), f"{setup}_{regime}_validation")
        worst_mean = min(float(item.get("mean_net_pct", -999.0)) for item in slices)
        positive_folds = sum(float(item.get("net_pct_points", 0.0)) > 0 for item in slices)
        objective = worst_mean + 0.35 * float(combined.get("mean_net_pct", -999.0))
        choices.append((positive_folds, objective, float(combined.get("profit_factor", 0.0)), floor, combined, slices))
    if not choices:
        return None, {"active": False, "reason": "insufficient_validation_samples"}
    selected = max(choices, key=lambda item: item[:3])
    combined = selected[4]
    active = bool(
        regime != "rotation"
        and selected[0] >= 2
        and int(combined.get("trades", 0)) >= 40
        and float(combined.get("profit_factor", 0.0)) >= 1.15
        and float(combined.get("net_pct_points", 0.0)) > 0
    )
    return (selected[3] if active else None), {
        "active": active,
        "reason": (
            "validated_positive_gate"
            if active
            else "rotation_noise_or_validation_expectancy_below_release_floor"
        ),
        "quantile_search": "0.85..0.995",
        "positive_validation_folds": selected[0],
        "objective": round(selected[1], 8),
        "combined": selected[4],
        "folds": selected[5],
    }


def cost_sensitivity(frame: pd.DataFrame) -> dict[str, Any]:
    output = {}
    for cost in (0.08, 0.12, 0.16, 0.20):
        values = frame.net_pct.astype(float) + ROUND_TRIP_COST_PCT - cost
        gains = float(values[values > 0].sum())
        losses = float(-values[values < 0].sum())
        output[f"{cost:.2f}"] = {
            "net_pct_points": round(float(values.sum()), 6),
            "profit_factor": round(gains / losses, 4) if losses else 999.0,
        }
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train the Binance-only S0 mixture of experts.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-train-per-expert", type=int, default=300_000)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    frame = prepare(pd.read_parquet(args.source))
    train_all = frame[frame.time < TRAIN_END - EMBARGO]
    validation = frame[(frame.time >= TRAIN_END) & (frame.time < VALID_END - EMBARGO)]
    test = frame[frame.time >= VALID_END]
    experts: dict[str, Expert] = {}
    fit_report: dict[str, Any] = {}
    for index, name in enumerate(EXPERTS):
        train = balanced_sample(
            train_all[train_all.setup_type.eq(name)],
            args.max_train_per_expert,
            410 + index,
        )
        valid = validation[validation.setup_type.eq(name)]
        expert = fit_expert(train, valid, 410 + index * 10)
        experts[name] = expert
        probability = expert.classifier.predict_proba(valid[list(FEATURES)])[:, 1]
        predicted_net = expert.regressor.predict(valid[list(FEATURES)])
        fit_report[name] = {
            "train_rows": len(train),
            "validation_rows": len(valid),
            "classifier_iterations": expert.classifier.best_iteration_,
            "regressor_iterations": expert.regressor.best_iteration_,
            "validation_auc": round(roc_auc_score(valid.win, probability), 6),
            "validation_log_loss": round(log_loss(valid.win, probability), 6),
            "validation_mae_pct": round(mean_absolute_error(valid.net_pct, predicted_net), 6),
        }

    validation = predict(validation, experts)
    test = predict(test, experts)
    floors: dict[str, float] = {}
    threshold_report: dict[str, Any] = {}
    for name in EXPERTS:
        for regime in REGIMES:
            key = gate_key(name, regime)
            floor, detail = choose_gate_floor(validation, name, regime)
            if floor is not None:
                floors[key] = floor
            threshold_report[key] = {"edge_floor": floor, **detail}

    selected_validation = schedule(validation, floors)
    selected_test = schedule(test, floors)
    report = {
        "experiment": "s0_binance_moe_v1",
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "scope": "S0 admission shadow challenger; Binance data only",
        "label": "10 minute path, 0.85 ATR stop, 1.05R take-profit, 0.12% round-trip cost",
        "data": {
            "source": str(args.source),
            "total": len(frame),
            "train_available": len(train_all),
            "validation": len(validation),
            "untouched_test": len(test),
            "symbols": int(frame.symbol.nunique()),
            "train_end": TRAIN_END.isoformat(),
            "validation_end": VALID_END.isoformat(),
        },
        "gate": (
            "setup_type routes to an expert; setup and market regime select an independently "
            "validated threshold; rotation/no-edge cells explicitly choose no trade"
        ),
        "fit": fit_report,
        "thresholds": threshold_report,
        "validation": {
            "v4_proxy": metrics(schedule(validation, {}, baseline=True), "v4_proxy"),
            "moe": metrics(selected_validation, "s0_binance_moe_v1"),
        },
        "untouched_test": {
            "v4_proxy": metrics(schedule(test, {}, baseline=True), "v4_proxy"),
            "moe": metrics(selected_test, "s0_binance_moe_v1"),
            "by_expert": {
            name: metrics(selected_test[selected_test.setup_type.eq(name)], name)
            for name in EXPERTS
            },
            "cost_sensitivity": cost_sensitivity(selected_test),
        },
    }
    eligible = (
        report["untouched_test"]["moe"]["trades"] >= 80
        and report["untouched_test"]["moe"]["profit_factor"] > 1.0
        and report["untouched_test"]["moe"]["net_pct_points"] > 0
        and report["validation"]["moe"]["profit_factor"] > 1.0
    )
    report["decision"] = "shadow_candidate" if eligible else "research_only_not_eligible"
    bundle = {
        "version": "s0_binance_moe_v1",
        "experts": {
            name: {
                "classifier": expert.classifier,
                "regressor": expert.regressor,
            }
            for name, expert in experts.items()
        },
        "gate_floors": floors,
        "features": list(FEATURES),
        "categorical": list(CATEGORICAL),
        "round_trip_cost_pct": ROUND_TRIP_COST_PCT,
        "decision": report["decision"],
        "training_summary": {
            "generated_at": report["generated_at"],
            "data": report["data"],
            "validation": report["validation"]["moe"],
            "untouched_test": report["untouched_test"]["moe"],
            "cost_sensitivity": report["untouched_test"]["cost_sensitivity"],
        },
    }
    joblib.dump(bundle, args.output / "s0_binance_moe_v1.joblib", compress=3)
    (args.output / "s0_binance_moe_v1_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
