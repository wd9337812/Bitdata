from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_s0_point_in_time_breakout import DEFAULT_PANEL
from scripts.benchmark_s0_point_in_time_state_model import (
    FEATURE_COLUMNS,
    add_model_features,
)


BASE_COST = 0.0012
STRESS_COST = 0.0024
HOLD_HOURS = 6
STOP_PCT = 0.006
TAKE_PCT = 0.010
WINDOWS = {
    "train": ("2026-01-01", "2026-04-01"),
    "validation": ("2026-04-01", "2026-06-01"),
    "test_june": ("2026-06-01", "2026-07-01"),
    "final_july": ("2026-07-01", "2026-08-01"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--panel", type=Path, default=DEFAULT_PANEL)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/research/s0_tail_event_model"),
    )
    return parser.parse_args()


def path_return(
    entry: np.ndarray,
    highs: list[np.ndarray],
    lows: list[np.ndarray],
    closes: list[np.ndarray],
    direction: int,
    cost: float = BASE_COST,
) -> np.ndarray:
    result = np.full(len(entry), np.nan, dtype=float)
    active = np.isfinite(entry)
    for high, low in zip(highs, lows, strict=True):
        if direction > 0:
            take = high >= entry * (1.0 + TAKE_PCT)
            stop = low <= entry * (1.0 - STOP_PCT)
        else:
            take = low <= entry * (1.0 - TAKE_PCT)
            stop = high >= entry * (1.0 + STOP_PCT)
        both = active & take & stop
        stopped = active & stop
        won = active & take & ~stop
        result[both | stopped] = -STOP_PCT - cost
        result[won] = TAKE_PCT - cost
        active &= ~(both | stopped | won)
    terminal = closes[-1]
    raw = direction * (terminal / entry - 1.0)
    result[active] = raw[active] - cost
    return result


def add_tail_labels(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.sort_values(["symbol", "available_ms"]).copy()
    grouped = result.groupby("symbol", sort=False)
    result["entry"] = grouped.open.shift(-1)
    highs = [grouped.high.shift(-step).to_numpy() for step in range(1, HOLD_HOURS + 1)]
    lows = [grouped.low.shift(-step).to_numpy() for step in range(1, HOLD_HOURS + 1)]
    closes = [grouped.close.shift(-step).to_numpy() for step in range(1, HOLD_HOURS + 1)]
    entry = result.entry.to_numpy()
    result["long_net"] = path_return(entry, highs, lows, closes, 1)
    result["short_net"] = path_return(entry, highs, lows, closes, -1)
    best = np.maximum(result.long_net, result.short_net)
    result["target"] = np.where(
        best <= 0,
        0,
        np.where(result.long_net >= result.short_net, 1, 2),
    ).astype("int8")
    result["label_exit_ms"] = result.available_ms + HOLD_HOURS * 3_600_000
    return result


def eligible(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.loc[
        ~frame.symbol.isin({"BTCUSDT", "ETHUSDT"})
        & frame.symbol_age_days.ge(30)
        & frame.liquidity_24h.ge(20_000_000)
    ].dropna(subset=[*FEATURE_COLUMNS, "long_net", "short_net", "entry"])


def window(frame: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    end_ms = int(pd.Timestamp(end, tz="UTC").timestamp() * 1000)
    return frame.loc[
        frame.available_ms.ge(start_ms)
        & frame.available_ms.lt(end_ms)
        & frame.label_exit_ms.lt(end_ms)
    ].copy()


def train_model(train: pd.DataFrame) -> lgb.LGBMClassifier:
    model = lgb.LGBMClassifier(
        objective="multiclass",
        num_class=3,
        n_estimators=360,
        learning_rate=0.025,
        num_leaves=15,
        max_depth=5,
        min_child_samples=1000,
        subsample=0.8,
        subsample_freq=1,
        colsample_bytree=0.75,
        reg_alpha=1.5,
        reg_lambda=8.0,
        class_weight="balanced",
        random_state=20260801,
        n_jobs=-1,
        verbosity=-1,
    )
    model.fit(
        train.loc[:, FEATURE_COLUMNS].astype("float32"),
        train.target,
    )
    return model


def add_predictions(model: lgb.LGBMClassifier, frame: pd.DataFrame) -> None:
    probabilities = model.predict_proba(
        frame.loc[:, FEATURE_COLUMNS].astype("float32")
    )
    frame["p_none"] = probabilities[:, 0]
    frame["p_long"] = probabilities[:, 1]
    frame["p_short"] = probabilities[:, 2]
    frame["confidence"] = np.maximum(frame.p_long, frame.p_short)
    frame["margin"] = np.abs(frame.p_long - frame.p_short)


def select_trades(frame: pd.DataFrame, threshold: float) -> pd.DataFrame:
    selected = frame.loc[
        frame.confidence.ge(threshold)
        & frame.margin.ge(0.05)
        & frame.confidence.gt(frame.p_none)
    ].copy()
    if selected.empty:
        return selected
    selected["direction"] = np.where(selected.p_long >= selected.p_short, "LONG", "SHORT")
    selected["net_return"] = np.where(
        selected.direction.eq("LONG"), selected.long_net, selected.short_net
    )
    selected = selected.sort_values(
        ["available_ms", "confidence", "liquidity_24h"],
        ascending=[True, False, False],
    ).groupby("available_ms", sort=False).head(1)
    # A single-position S0 account cannot open overlapping six-hour paths.
    kept = []
    next_free = -1
    for row in selected.sort_values("available_ms").itertuples():
        if row.available_ms >= next_free:
            kept.append(row.Index)
            next_free = row.available_ms + HOLD_HOURS * 3_600_000
    return selected.loc[kept].sort_values("available_ms").reset_index(drop=True)


def metrics(trades: pd.DataFrame, extra_cost: float = 0.0) -> dict[str, float | int]:
    values = trades.net_return.astype(float) - extra_cost
    wins = values[values > 0]
    losses = values[values < 0]
    gross_loss = float(-losses.sum())
    equity = (1.0 + values).cumprod()
    drawdown = equity / equity.cummax() - 1.0 if len(equity) else pd.Series(dtype=float)
    return {
        "trades": int(len(values)),
        "win_rate": float((values > 0).mean()) if len(values) else 0.0,
        "profit_factor": float(wins.sum() / gross_loss) if gross_loss else (999.0 if len(wins) else 0.0),
        "net_return": float(values.sum()),
        "compounded_return": float(equity.iloc[-1] - 1.0) if len(equity) else 0.0,
        "max_drawdown": float(drawdown.min()) if len(drawdown) else 0.0,
    }


def threshold_report(frame: pd.DataFrame, threshold: float) -> tuple[dict, pd.DataFrame]:
    trades = select_trades(frame, threshold)
    return {
        "threshold": threshold,
        "base": metrics(trades),
        "stress": metrics(trades, STRESS_COST - BASE_COST),
    }, trades


def choose_threshold(reports: list[dict]) -> float | None:
    qualified = [
        item for item in reports
        if item["base"]["trades"] >= 30
        and item["base"]["profit_factor"] >= 1.10
        and item["stress"]["net_return"] > 0
    ]
    if not qualified:
        return None
    return float(max(qualified, key=lambda item: (item["stress"]["profit_factor"], item["stress"]["net_return"]))["threshold"])


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    panel = pd.read_parquet(args.panel)
    featured = eligible(add_tail_labels(add_model_features(panel)))
    splits = {name: window(featured, *bounds) for name, bounds in WINDOWS.items()}
    model = train_model(splits["train"])
    for frame in splits.values():
        add_predictions(model, frame)
    quantiles = (0.90, 0.95, 0.975, 0.99)
    thresholds = [float(splits["train"].confidence.quantile(q)) for q in quantiles]
    validation_reports = [threshold_report(splits["validation"], value)[0] for value in thresholds]
    frozen = choose_threshold(validation_reports)
    evaluations = {}
    all_trades = []
    if frozen is not None:
        for name in ("test_june", "final_july"):
            report, trades = threshold_report(splits[name], frozen)
            evaluations[name] = report
            trades["window"] = name
            all_trades.append(trades)
    passed = bool(
        frozen is not None
        and all(evaluations[name]["base"]["trades"] >= 20 for name in evaluations)
        and all(evaluations[name]["base"]["net_return"] > 0 for name in evaluations)
        and all(evaluations[name]["stress"]["net_return"] > 0 for name in evaluations)
        and all(evaluations[name]["base"]["profit_factor"] >= 1.10 for name in evaluations)
    )
    report = {
        "experiment": "s0_tail_event_model",
        "method": "Point-in-time three-class model: no trade, long tail, short tail. Labels use next-hour entry and deterministic six-hour stop/take path.",
        "rows": {name: int(len(frame)) for name, frame in splits.items()},
        "symbols": int(featured.symbol.nunique()),
        "costs": {"base": BASE_COST, "stress": STRESS_COST},
        "path": {"hold_hours": HOLD_HOURS, "stop_pct": STOP_PCT, "take_pct": TAKE_PCT},
        "threshold_validation": validation_reports,
        "frozen_threshold": frozen,
        "frozen_evaluation": evaluations,
        "accepted": passed,
        "decision": "stage_one_pass_requires_broader_years" if passed else "rejected_not_positive_expectancy",
    }
    if all_trades:
        pd.concat(all_trades, ignore_index=True).to_parquet(args.output / "frozen_trades.parquet", index=False)
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
