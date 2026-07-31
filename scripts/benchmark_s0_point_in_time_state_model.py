from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_s0_xmom_regime_candidates import (
    BASE_COST,
    STRESS_COST,
    block_bootstrap,
    cost_adjusted,
    scope,
)
from scripts.benchmark_s0_cross_sectional_momentum import (
    Profile,
    simulate,
    summarize,
)
from scripts.benchmark_s0_point_in_time_breakout import (
    DEFAULT_PANEL,
    add_breakout_features,
)
from scripts.benchmark_s0_xmom_point_in_time import (
    DEFAULT_DATA,
    load_manifest,
)


DEFAULT_OUTPUT = (
    ROOT / "data" / "research" / "s0_point_in_time_state_model"
)
HOLD_HOURS = 4
FEATURE_COLUMNS = (
    "ret_1h",
    "ret_2h",
    "ret_4h",
    "ret_6h",
    "ret_12h",
    "ret_24h",
    "ret_72h",
    "atr_pct",
    "volume_ratio",
    "log_liquidity",
    "log_age",
    "ret_1h_rank",
    "ret_4h_rank",
    "ret_24h_rank",
    "volume_rank",
    "btc_ret_1h",
    "btc_ret_4h",
    "btc_ret_24h",
    "breadth_ret_1h",
    "breadth_ret_4h",
    "breadth_ret_24h",
    "market_positive_share",
    "hour_sin",
    "hour_cos",
)
THRESHOLD_QUANTILES = (0.90, 0.95, 0.975, 0.99)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Train a regularized point-in-time state model on Jan-Mar, "
            "calibrate only on Apr-May, then freeze for June and July."
        )
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--panel", type=Path, default=DEFAULT_PANEL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--minimum-age-days", type=int, default=30)
    parser.add_argument(
        "--minimum-liquidity-24h",
        type=float,
        default=20_000_000,
    )
    return parser.parse_args()


def add_model_features(
    panel: pd.DataFrame,
    hold_hours: int = HOLD_HOURS,
) -> pd.DataFrame:
    result = add_breakout_features(panel, ())
    result = result.sort_values(["symbol", "available_ms"]).copy()
    grouped = result.groupby("symbol", sort=False)
    for hours in (1, 2, 4, 12):
        result[f"ret_{hours}h"] = grouped.close.pct_change(hours)
    result["atr_pct"] = result.atr_24h / result.close
    result["log_liquidity"] = np.log1p(result.liquidity_24h)
    result["log_age"] = np.log1p(result.symbol_age_days.clip(lower=0))
    for column in ("ret_1h", "ret_4h", "ret_24h", "volume_ratio"):
        result[f"{column.replace('volume_ratio', 'volume')}_rank"] = (
            result.groupby("available_ms", sort=False)[column].rank(pct=True)
        )
    eligible_alt = result.loc[
        ~result.symbol.isin({"BTCUSDT", "ETHUSDT"})
        & result.liquidity_24h.ge(5_000_000)
    ]
    btc = result.loc[result.symbol.eq("BTCUSDT")].set_index("available_ms")
    for hours in (1, 4, 24):
        result[f"btc_ret_{hours}h"] = result.available_ms.map(
            btc[f"ret_{hours}h"]
        )
        breadth = eligible_alt.groupby(
            "available_ms",
            sort=False,
        )[f"ret_{hours}h"].median()
        result[f"breadth_ret_{hours}h"] = result.available_ms.map(breadth)
    positive_share = eligible_alt.groupby(
        "available_ms",
        sort=False,
    ).ret_24h.apply(lambda values: float(values.gt(0).mean()))
    result["market_positive_share"] = result.available_ms.map(positive_share)
    timestamp = pd.to_datetime(result.available_ms, unit="ms", utc=True)
    hour = timestamp.dt.hour + timestamp.dt.minute / 60.0
    result["hour_sin"] = np.sin(2 * np.pi * hour / 24.0)
    result["hour_cos"] = np.cos(2 * np.pi * hour / 24.0)
    result["entry_open"] = grouped.open.shift(-1)
    result["label_exit_close"] = grouped.close.shift(-hold_hours)
    result["future_return"] = (
        result.label_exit_close / result.entry_open - 1.0
    ).clip(-0.20, 0.20)
    result["label_exit_ms"] = (
        result.available_ms.astype("int64") + hold_hours * 3_600_000
    )
    return result.sort_values(["available_ms", "symbol"]).reset_index(drop=True)


def eligible_rows(
    frame: pd.DataFrame,
    minimum_age_days: int,
    minimum_liquidity_24h: float,
) -> pd.DataFrame:
    return frame.loc[
        ~frame.symbol.isin({"BTCUSDT", "ETHUSDT"})
        & frame.symbol_age_days.ge(minimum_age_days)
        & frame.liquidity_24h.ge(minimum_liquidity_24h)
    ].dropna(
        subset=[*FEATURE_COLUMNS, "future_return", "entry_open"]
    )


def purged_window(
    frame: pd.DataFrame,
    start: str,
    end: str,
) -> pd.DataFrame:
    start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    end_ms = int(pd.Timestamp(end, tz="UTC").timestamp() * 1000)
    return frame.loc[
        frame.available_ms.ge(start_ms)
        & frame.available_ms.lt(end_ms)
        & frame.label_exit_ms.lt(end_ms)
    ].copy()


def train_model(train: pd.DataFrame) -> lgb.LGBMRegressor:
    model = lgb.LGBMRegressor(
        objective="huber",
        n_estimators=320,
        learning_rate=0.025,
        num_leaves=15,
        max_depth=5,
        min_child_samples=800,
        subsample=0.8,
        subsample_freq=1,
        colsample_bytree=0.75,
        reg_alpha=0.20,
        reg_lambda=1.50,
        random_state=20260731,
        n_jobs=-1,
        verbosity=-1,
    )
    model.fit(
        train.loc[:, FEATURE_COLUMNS].astype("float32"),
        train.future_return.astype("float32"),
    )
    return model


def prediction_signals(
    frame: pd.DataFrame,
    prediction_threshold: float,
) -> pd.DataFrame:
    selected = frame.loc[
        frame.predicted_return.abs().ge(prediction_threshold)
    ].copy()
    if selected.empty:
        return selected
    selected["direction"] = np.where(
        selected.predicted_return.gt(0),
        "LONG",
        "SHORT",
    )
    selected["strength"] = selected.predicted_return.abs()
    selected = (
        selected.sort_values(
            ["available_ms", "strength", "liquidity_24h"],
            ascending=[True, False, False],
        )
        .groupby("available_ms", sort=False)
        .head(1)
        .copy()
    )
    selected["market_direction"] = selected.direction
    return selected.sort_values("available_ms").reset_index(drop=True)


def strategy_profile() -> Profile:
    return Profile(
        name="point_in_time_state_model",
        score_columns=("ret_24h",),
        quantile=0.0,
        stop_atr=1.5,
        reward_r=1.5,
        hold_hours=HOLD_HOURS,
    )


def evaluate_threshold(
    frame: pd.DataFrame,
    panel: pd.DataFrame,
    threshold: float,
) -> tuple[dict[str, Any], pd.DataFrame]:
    signals = prediction_signals(frame, threshold)
    trades = simulate(
        signals,
        panel,
        strategy_profile(),
        cost_pct=BASE_COST,
    )
    stressed = cost_adjusted(trades, STRESS_COST)
    return {
        "prediction_threshold": threshold,
        "signals": int(len(signals)),
        "base": summarize(trades),
        "stress": summarize(stressed),
    }, trades


def choose_validation_threshold(
    reports: dict[str, dict[str, Any]],
    minimum_trades: int = 40,
) -> str | None:
    qualified = [
        (name, item)
        for name, item in reports.items()
        if item["base"].get("trades", 0) >= minimum_trades
        and item["base"].get("profit_factor", 0) >= 1.08
        and item["stress"].get("profit_factor", 0) > 1.0
        and item["stress"].get("net_pct_points", 0) > 0
    ]
    if not qualified:
        return None
    qualified.sort(
        key=lambda pair: (
            pair[1]["stress"].get("profit_factor", 0),
            pair[1]["stress"].get("net_pct_points", 0),
            pair[1]["base"].get("trades", 0),
        ),
        reverse=True,
    )
    return qualified[0][0]


def frozen_window_report(
    trades: pd.DataFrame,
    start: str,
    end: str,
    *,
    bootstrap: bool = False,
) -> dict[str, Any]:
    base = scope(trades, start, end)
    stress = cost_adjusted(base, STRESS_COST)
    report: dict[str, Any] = {
        "base": summarize(base),
        "stress": summarize(stress),
    }
    if bootstrap:
        report["weekly_block_bootstrap_base"] = block_bootstrap(base)
        report["weekly_block_bootstrap_stress"] = block_bootstrap(stress)
    return report


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    _, manifest = load_manifest(args.data)
    panel = pd.read_parquet(args.panel)
    featured = add_model_features(panel)
    eligible = eligible_rows(
        featured,
        args.minimum_age_days,
        args.minimum_liquidity_24h,
    )
    train = purged_window(eligible, "2026-01-01", "2026-04-01")
    validation = purged_window(eligible, "2026-04-01", "2026-06-01")
    test = purged_window(eligible, "2026-06-01", "2026-07-01")
    final = purged_window(eligible, "2026-07-01", "2026-08-01")
    model = train_model(train)
    for frame in (train, validation, test, final):
        frame["predicted_return"] = model.predict(
            frame.loc[:, FEATURE_COLUMNS].astype("float32")
        )
    train_abs_prediction = np.abs(train.predicted_return.to_numpy())
    thresholds = {
        f"q{int(quantile * 1000):03d}": float(
            np.quantile(train_abs_prediction, quantile)
        )
        for quantile in THRESHOLD_QUANTILES
    }
    validation_reports: dict[str, dict[str, Any]] = {}
    for name, threshold in thresholds.items():
        report, _ = evaluate_threshold(validation, panel, threshold)
        validation_reports[name] = report
    frozen_threshold_name = choose_validation_threshold(validation_reports)
    frozen_threshold = (
        thresholds[frozen_threshold_name] if frozen_threshold_name else None
    )
    frozen_trades: list[pd.DataFrame] = []
    frozen_evidence: dict[str, Any] = {}
    if frozen_threshold is not None:
        for name, frame, start, end in (
            ("test_june", test, "2026-06-01", "2026-07-01"),
            ("final_july", final, "2026-07-01", "2026-08-01"),
        ):
            _, trades = evaluate_threshold(frame, panel, frozen_threshold)
            trades["window"] = name
            frozen_trades.append(trades)
            frozen_evidence[name] = frozen_window_report(
                trades,
                start,
                end,
                bootstrap=name == "final_july",
            )
        combined = pd.concat(frozen_trades, ignore_index=True)
        frozen_evidence["combined_june_july"] = {
            "base": summarize(combined),
            "stress": summarize(cost_adjusted(combined, STRESS_COST)),
        }
    else:
        combined = pd.DataFrame()
    passed = bool(
        frozen_threshold_name
        and frozen_evidence["test_june"]["base"].get("net_pct_points", 0) > 0
        and frozen_evidence["final_july"]["base"].get("net_pct_points", 0) > 0
        and frozen_evidence["combined_june_july"]["stress"].get(
            "profit_factor",
            0,
        )
        > 1.0
    )
    importance = sorted(
        zip(FEATURE_COLUMNS, model.feature_importances_, strict=True),
        key=lambda item: int(item[1]),
        reverse=True,
    )
    report = {
        "method": (
            "Regularized LightGBM Huber regression without symbol identity. "
            "Features use completed bars only; label starts at next-hour open; "
            "time splits purge labels crossing their end boundary."
        ),
        "data": {
            "monthly_source": manifest.get("source"),
            "daily_extension": manifest.get("daily_extension"),
            "panel_rows": int(len(panel)),
            "eligible_rows": int(len(eligible)),
            "symbols": int(eligible.symbol.nunique()),
            "train_rows": int(len(train)),
            "validation_rows": int(len(validation)),
            "test_rows": int(len(test)),
            "final_rows": int(len(final)),
        },
        "model": {
            "type": "LGBMRegressor",
            "parameters": model.get_params(),
            "features": list(FEATURE_COLUMNS),
            "feature_importance": [
                {"feature": name, "splits": int(value)}
                for name, value in importance
            ],
            "symbol_identity_used": False,
        },
        "selection": {
            "threshold_source": "train absolute-prediction quantiles",
            "thresholds": thresholds,
            "validation_reports": validation_reports,
            "frozen_threshold_name": frozen_threshold_name,
            "frozen_threshold": frozen_threshold,
        },
        "frozen_evaluation": frozen_evidence,
        "hourly_research_passed": passed,
        "live_qualified": False,
        "live_qualification_reason": (
            "Minute-level execution, probability calibration, and shadow "
            "validation are still required."
            if passed
            else "Validation or frozen test windows did not pass."
        ),
    }
    if not combined.empty:
        combined.to_parquet(
            args.output / "frozen_trades.parquet",
            index=False,
        )
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
