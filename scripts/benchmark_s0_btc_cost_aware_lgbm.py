from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = (
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2024_2025"
)
DEFAULT_EXTENSION = (
    ROOT / "data" / "research" / "binance_um_point_in_time_1h"
)
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_btc_cost_aware_lgbm"
BASE_COST = 0.0006
STRESS_COST = 0.0012
LAMBDAS = (1.0, 2.0, 3.0)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Walk-forward audit of cost-aware hourly BTC forecasts."
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--extension", type=Path, default=DEFAULT_EXTENSION)
    parser.add_argument(
        "--history",
        type=Path,
        action="append",
        default=[],
        help="Optional earlier point-in-time archive roots, oldest first.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def load_btc(
    data: Path, extension: Path, history: tuple[Path, ...] = ()
) -> pd.DataFrame:
    columns = [
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "quote_volume",
        "taker_buy_quote_volume",
    ]
    paths = [root / "parquet" / "BTCUSDT.parquet" for root in history]
    paths.append(data / "parquet" / "BTCUSDT.parquet")
    extra = extension / "parquet" / "BTCUSDT.parquet"
    if extra.exists():
        paths.append(extra)
    frame = (
        pd.concat(
            [pd.read_parquet(path, columns=columns) for path in paths],
            ignore_index=True,
        )
        .drop_duplicates("open_time", keep="last")
        .sort_values("open_time")
        .reset_index(drop=True)
    )
    frame["time"] = pd.to_datetime(frame.open_time, unit="ms", utc=True)
    return frame


def rsi(series: pd.Series, period: int) -> pd.Series:
    change = series.diff()
    gain = change.clip(lower=0).rolling(period).mean()
    loss = -change.clip(upper=0).rolling(period).mean()
    strength = gain / loss.replace(0, np.nan)
    value = 100.0 - 100.0 / (1.0 + strength)
    value = value.mask(loss.eq(0) & gain.gt(0), 100.0)
    value = value.mask(gain.eq(0) & loss.gt(0), 0.0)
    return value.mask(gain.eq(0) & loss.eq(0), 50.0)


def build_features(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    result = frame.copy()
    log_close = np.log(result.close)
    feature_names: list[str] = []
    for lag in (1, 2, 3, 6, 12, 24, 48, 72, 168, 336):
        name = f"return_{lag}h"
        result[name] = log_close.diff(lag)
        feature_names.append(name)
    result["range_ratio"] = (result.high - result.low) / result.close
    result["body_ratio"] = (result.close - result.open) / result.open
    result["taker_imbalance"] = (
        2.0 * result.taker_buy_quote_volume
        / result.quote_volume.replace(0, np.nan)
        - 1.0
    )
    log_volume = np.log1p(result.quote_volume)
    for period in (24, 168):
        result[f"volume_z_{period}"] = (
            log_volume - log_volume.rolling(period).mean()
        ) / log_volume.rolling(period).std().replace(0, np.nan)
        result[f"volatility_{period}"] = result.return_1h.rolling(period).std()
        result[f"distance_sma_{period}"] = (
            result.close / result.close.rolling(period).mean() - 1.0
        )
        feature_names.extend(
            [
                f"volume_z_{period}",
                f"volatility_{period}",
                f"distance_sma_{period}",
            ]
        )
    previous_close = result.close.shift(1)
    true_range = pd.concat(
        [
            result.high - result.low,
            (result.high - previous_close).abs(),
            (result.low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    result["atr_ratio_14"] = true_range.rolling(14).mean() / result.close
    result["rsi_14"] = rsi(result.close, 14) / 100.0
    feature_names.extend(
        ["range_ratio", "body_ratio", "taker_imbalance", "atr_ratio_14", "rsi_14"]
    )
    # A completed bar at t can only be acted on at the next hourly open.
    result["target"] = result.open.shift(-2) / result.open.shift(-1) - 1.0
    return result.dropna(subset=feature_names + ["target"]), feature_names


def cost_aware_positions(
    predictions: np.ndarray,
    mode: str,
    one_way_cost: float,
    threshold_multiplier: float,
) -> np.ndarray:
    positions = np.zeros(len(predictions), dtype=float)
    current = 0.0
    for index, prediction in enumerate(predictions):
        desired = (
            1.0 if prediction > 0 else (0.0 if mode == "LONG_ONLY" else -1.0)
        )
        turnover = abs(desired - current)
        if turnover > 0 and abs(prediction) > threshold_multiplier * one_way_cost * turnover:
            current = desired
        positions[index] = current
    return positions


def strategy_returns(
    predictions: np.ndarray,
    realised: np.ndarray,
    mode: str,
    one_way_cost: float,
    threshold_multiplier: float,
) -> pd.DataFrame:
    positions = cost_aware_positions(
        predictions, mode, one_way_cost, threshold_multiplier
    )
    previous = np.r_[0.0, positions[:-1]]
    turnover = np.abs(positions - previous)
    net = positions * realised - turnover * one_way_cost
    if len(net) and positions[-1] != 0:
        net[-1] -= abs(positions[-1]) * one_way_cost
    return pd.DataFrame(
        {
            "prediction": predictions,
            "realised": realised,
            "position": positions,
            "turnover": turnover,
            "net_return": net,
        }
    )


def metrics(returns: pd.DataFrame) -> dict[str, float | int]:
    if returns.empty:
        return {"hours": 0, "trades": 0, "net_return": 0.0, "sharpe": 0.0, "max_drawdown": 0.0}
    net = returns.net_return
    equity = (1.0 + net).cumprod()
    drawdown = equity / equity.cummax() - 1.0
    std = net.std()
    return {
        "hours": int(len(returns)),
        "trades": int((returns.turnover > 0).sum()),
        "net_return": float(equity.iloc[-1] - 1.0),
        "sharpe": float(net.mean() / std * math.sqrt(8760)) if std > 0 else 0.0,
        "max_drawdown": float(drawdown.min()),
    }


def yearly_metrics(returns: pd.DataFrame) -> dict[str, dict[str, float | int]]:
    if returns.empty:
        return {}
    frame = returns.copy()
    frame["year"] = pd.to_datetime(frame.time, utc=True).dt.year
    return {
        str(int(year)): metrics(group.drop(columns="year"))
        for year, group in frame.groupby("year", sort=True)
    }


def fold_boundaries(frame: pd.DataFrame) -> list[dict[str, pd.Timestamp]]:
    first_observation = frame.time.min()
    first = pd.Timestamp(
        year=int(first_observation.year), month=1, day=1, tz="UTC"
    )
    last = frame.time.max().floor("D")
    folds: list[dict[str, pd.Timestamp]] = []
    test_start = first + pd.DateOffset(months=15)
    while test_start + pd.DateOffset(months=3) <= last:
        folds.append(
            {
                "train_start": test_start - pd.DateOffset(months=15),
                "train_end": test_start - pd.DateOffset(months=3),
                "validation_start": test_start - pd.DateOffset(months=3),
                "test_start": test_start,
                "test_end": test_start + pd.DateOffset(months=3),
            }
        )
        test_start += pd.DateOffset(months=3)
    return folds


def fit_model(train: pd.DataFrame, features: list[str]) -> lgb.LGBMRegressor:
    model = lgb.LGBMRegressor(
        objective="regression_l1",
        n_estimators=350,
        learning_rate=0.025,
        num_leaves=15,
        max_depth=5,
        min_child_samples=100,
        subsample=0.8,
        colsample_bytree=0.8,
        reg_alpha=0.2,
        reg_lambda=1.0,
        random_state=42,
        verbosity=-1,
        n_jobs=4,
    )
    model.fit(train[features], train.target)
    return model


def choose_rule(
    predictions: np.ndarray,
    realised: np.ndarray,
) -> tuple[str, float, dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for mode in ("LONG_ONLY", "LONG_SHORT"):
        for multiplier in LAMBDAS:
            result = strategy_returns(
                predictions, realised, mode, BASE_COST, multiplier
            )
            candidates.append(
                {
                    "mode": mode,
                    "lambda": multiplier,
                    **metrics(result),
                }
            )
    chosen = max(
        candidates,
        key=lambda item: (item["net_return"], item["sharpe"], -item["trades"]),
    )
    return str(chosen["mode"]), float(chosen["lambda"]), chosen


def main() -> None:
    args = parse_args()
    frame, features = build_features(
        load_btc(args.data, args.extension, tuple(args.history))
    )
    fold_reports: list[dict[str, Any]] = []
    base_parts: list[pd.DataFrame] = []
    stress_parts: list[pd.DataFrame] = []
    for index, bounds in enumerate(fold_boundaries(frame), start=1):
        train = frame.loc[
            frame.time.ge(bounds["train_start"]) & frame.time.lt(bounds["train_end"])
        ]
        validation = frame.loc[
            frame.time.ge(bounds["validation_start"]) & frame.time.lt(bounds["test_start"])
        ]
        test = frame.loc[
            frame.time.ge(bounds["test_start"]) & frame.time.lt(bounds["test_end"])
        ]
        model = fit_model(train, features)
        validation_predictions = model.predict(validation[features])
        mode, multiplier, chosen_validation = choose_rule(
            validation_predictions, validation.target.to_numpy()
        )
        combined = pd.concat([train, validation], ignore_index=True)
        model = fit_model(combined, features)
        predictions = model.predict(test[features])
        base = strategy_returns(
            predictions, test.target.to_numpy(), mode, BASE_COST, multiplier
        )
        stress = strategy_returns(
            predictions, test.target.to_numpy(), mode, STRESS_COST, multiplier
        )
        base["time"] = test.time.to_numpy()
        stress["time"] = test.time.to_numpy()
        base_parts.append(base)
        stress_parts.append(stress)
        fold_reports.append(
            {
                "fold": index,
                **{key: str(value) for key, value in bounds.items()},
                "selected_mode": mode,
                "selected_lambda": multiplier,
                "validation": chosen_validation,
                "base": metrics(base),
                "stress": metrics(stress),
            }
        )

    base_all = pd.concat(base_parts, ignore_index=True)
    stress_all = pd.concat(stress_parts, ignore_index=True)
    base_metrics = metrics(base_all)
    stress_metrics = metrics(stress_all)
    accepted = bool(
        len(fold_reports) >= 4
        and base_metrics["net_return"] > 0
        and stress_metrics["net_return"] > 0
        and base_metrics["sharpe"] >= 0.75
        and sum(report["base"]["net_return"] > 0 for report in fold_reports)
        >= math.ceil(len(fold_reports) * 0.67)
        and sum(report["stress"]["net_return"] > 0 for report in fold_reports)
        >= math.ceil(len(fold_reports) * 0.67)
    )
    report = {
        "experiment": "s0_btc_cost_aware_lgbm",
        "source_method": "Bysik-Slepaczuk cost-aware hourly forecast proxy",
        "data_start": frame.time.min().isoformat(),
        "data_end": frame.time.max().isoformat(),
        "execution": "features at close t; trade next open; earn next open-to-open return",
        "feature_count": len(features),
        "features": features,
        "base_one_way_cost": BASE_COST,
        "stress_one_way_cost": STRESS_COST,
        "folds": fold_reports,
        "base": base_metrics,
        "stress": stress_metrics,
        "base_yearly": yearly_metrics(base_all),
        "stress_yearly": yearly_metrics(stress_all),
        "fold_stability": {
            "folds": len(fold_reports),
            "base_positive_folds": sum(
                item["base"]["net_return"] > 0 for item in fold_reports
            ),
            "stress_positive_folds": sum(
                item["stress"]["net_return"] > 0 for item in fold_reports
            ),
        },
        "accepted": accepted,
        "decision": (
            "eligible_for_further_shadow_validation"
            if accepted
            else "rejected_not_positive_expectancy"
        ),
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    base_all.to_parquet(args.output / "base_returns.parquet", index=False)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
