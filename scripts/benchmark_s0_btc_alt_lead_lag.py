from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data" / "research" / "s0_public_1m" / "parquet"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_btc_alt_lead_lag"
PAPER_ONE_WAY_COST = 0.0002
BASE_ONE_WAY_COST = 0.0006
STRESS_ONE_WAY_COST = 0.0012
ENTRY_THRESHOLDS = (-0.0001, 0.0, 0.0001, 0.0002)
HOLD_THRESHOLDS = (-0.0002, -0.0001, 0.0, 0.0001)
FEATURES = ("btc_return", "alt_return")
MATURE_UNIVERSE = (
    "1000SHIBUSDT", "1000XECUSDT", "AAVEUSDT", "ADAUSDT", "ATOMUSDT",
    "AVAXUSDT", "BCHUSDT", "BNBUSDT", "CRVUSDT", "DASHUSDT", "DOGEUSDT",
    "DOTUSDT", "ETCUSDT", "FILUSDT", "HBARUSDT", "ICPUSDT", "LINKUSDT",
    "LITUSDT", "LTCUSDT", "NEARUSDT", "ONEUSDT", "SOLUSDT", "TLMUSDT",
    "TRXUSDT", "UNIUSDT", "XLMUSDT", "XMRUSDT", "XRPUSDT", "ZECUSDT",
    "ZILUSDT",
)


@dataclass(frozen=True)
class Fold:
    month: str
    fit_start: pd.Timestamp
    validation_start: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Point-in-time audit of BTC-to-low-liquidity-alt one-minute lag."
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--start", default="2026-01")
    parser.add_argument("--end", default="2026-06")
    parser.add_argument("--low-count", type=int, default=8)
    parser.add_argument("--control-count", type=int, default=4)
    return parser.parse_args()


def fold_for_month(month: str) -> Fold:
    start = pd.Timestamp(f"{month}-01", tz="UTC")
    return Fold(
        month=month,
        fit_start=start,
        validation_start=start + pd.Timedelta(days=14),
        test_start=start + pd.Timedelta(days=21),
        test_end=start + pd.offsets.MonthBegin(1),
    )


def read_window(path: Path, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    columns = ["open_time", "open", "close", "trades"]
    frame = pd.read_parquet(
        path,
        columns=columns,
        filters=[("open_time", ">=", start_ms), ("open_time", "<", end_ms)],
    )
    frame = frame.sort_values("open_time").drop_duplicates("open_time", keep="last")
    return frame.reset_index(drop=True)


def select_liquidity_buckets(
    data: Path, fold: Fold, low_count: int, control_count: int
) -> tuple[list[str], list[str], dict[str, int]]:
    totals: dict[str, int] = {}
    for symbol in MATURE_UNIVERSE:
        path = data / f"{symbol}.parquet"
        if not path.exists():
            continue
        frame = read_window(path, fold.fit_start, fold.test_start)
        if len(frame) >= 20 * 24 * 60:
            totals[symbol] = int(frame.trades.sum())
    ordered = sorted(totals, key=lambda symbol: (totals[symbol], symbol))
    low = ordered[: max(1, low_count)]
    controls = list(reversed(ordered[-max(1, control_count) :]))
    return low, controls, totals


def build_pair(alt: pd.DataFrame, btc: pd.DataFrame) -> pd.DataFrame:
    merged = alt.merge(
        btc.loc[:, ["open_time", "close"]].rename(columns={"close": "btc_close"}),
        on="open_time",
        how="inner",
    ).sort_values("open_time")
    merged["time"] = pd.to_datetime(merged.open_time, unit="ms", utc=True)
    merged["btc_return"] = np.log(merged.btc_close).diff()
    merged["alt_return"] = np.log(merged.close).diff()
    # A completed minute t is observable before the next open. The executable
    # target is open(t+1) to open(t+2), never close(t) to close(t+1).
    merged["target"] = np.log(merged.open.shift(-2) / merged.open.shift(-1))
    return merged.dropna(subset=[*FEATURES, "target"]).reset_index(drop=True)


def fit_classifier(frame: pd.DataFrame, threshold: float) -> lgb.LGBMClassifier | float:
    labels = frame.target.gt(threshold).astype(int)
    if labels.nunique() < 2:
        return float(labels.iloc[0])
    model = lgb.LGBMClassifier(
        objective="binary",
        n_estimators=80,
        learning_rate=0.05,
        num_leaves=7,
        max_depth=3,
        min_child_samples=200,
        subsample=0.8,
        colsample_bytree=1.0,
        reg_alpha=0.2,
        reg_lambda=1.0,
        random_state=42,
        verbosity=-1,
        n_jobs=2,
    )
    model.fit(frame.loc[:, FEATURES], labels)
    return model


def predict_probability(model: lgb.LGBMClassifier | float, frame: pd.DataFrame) -> np.ndarray:
    if isinstance(model, float):
        return np.full(len(frame), model, dtype=float)
    return model.predict_proba(frame.loc[:, FEATURES])[:, 1]


def simulate_long(
    target: np.ndarray,
    entry_probability: np.ndarray,
    hold_probability: np.ndarray,
    one_way_cost: float,
) -> pd.DataFrame:
    held = False
    net = np.zeros(len(target), dtype=float)
    turnover = np.zeros(len(target), dtype=float)
    position = np.zeros(len(target), dtype=float)
    for index in range(len(target)):
        desired = entry_probability[index] >= 0.5 if not held else hold_probability[index] >= 0.5
        if desired != held:
            turnover[index] = 1.0
            net[index] -= one_way_cost
        held = bool(desired)
        position[index] = 1.0 if held else 0.0
        if held:
            net[index] += target[index]
    if held and len(net):
        turnover[-1] += 1.0
        net[-1] -= one_way_cost
    return pd.DataFrame({"net": net, "turnover": turnover, "position": position})


def metrics(net: pd.Series, turnover: pd.Series) -> dict[str, float | int]:
    values = net.astype(float)
    gains = float(values.loc[values.gt(0)].sum())
    losses = float(-values.loc[values.lt(0)].sum())
    equity = np.exp(values.cumsum())
    drawdown = equity / equity.cummax() - 1.0
    return {
        "minutes": int(len(values)),
        "round_trips": int(round(float(turnover.sum()) / 2.0)),
        "active_minutes": int(values.ne(0).sum()),
        "win_minute_pct": float(values.gt(0).mean() * 100.0) if len(values) else 0.0,
        "profit_factor": gains / losses if losses else (999.0 if gains else 0.0),
        "net_pct_points": float(values.sum() * 100.0),
        "compounded_return_pct": float((equity.iloc[-1] - 1.0) * 100.0) if len(equity) else 0.0,
        "max_drawdown_pct": float(drawdown.min() * 100.0) if len(drawdown) else 0.0,
    }


def train_predictions(
    frame: pd.DataFrame, train: pd.DataFrame, thresholds: tuple[float, ...]
) -> dict[float, np.ndarray]:
    return {
        threshold: predict_probability(fit_classifier(train, threshold), frame)
        for threshold in thresholds
    }


def choose_thresholds(
    predictions: dict[str, dict[str, dict[float, np.ndarray]]],
    validation: dict[str, pd.DataFrame],
) -> tuple[float, float, list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    for entry in ENTRY_THRESHOLDS:
        for hold in HOLD_THRESHOLDS:
            symbol_returns = []
            trades = 0
            for symbol, frame in validation.items():
                result = simulate_long(
                    frame.target.to_numpy(),
                    predictions[symbol]["entry"][entry],
                    predictions[symbol]["hold"][hold],
                    PAPER_ONE_WAY_COST,
                )
                symbol_returns.append(float(result.net.sum()))
                trades += int(round(result.turnover.sum() / 2.0))
            candidates.append(
                {
                    "entry": entry,
                    "hold": hold,
                    "mean_net_log_return": float(np.mean(symbol_returns)),
                    "round_trips": trades,
                }
            )
    chosen = max(candidates, key=lambda item: (item["mean_net_log_return"], -item["round_trips"]))
    return float(chosen["entry"]), float(chosen["hold"]), candidates


def aggregate_symbol_results(parts: list[pd.DataFrame], cost_name: str) -> dict[str, Any]:
    if not parts:
        return metrics(pd.Series(dtype=float), pd.Series(dtype=float))
    combined = pd.concat(parts, ignore_index=True)
    grouped = combined.groupby("open_time", sort=True).agg(net=(cost_name, "mean"), turnover=("turnover", "mean"))
    return metrics(grouped.net, grouped.turnover)


def run_fold(data: Path, fold: Fold, low_count: int, control_count: int) -> dict[str, Any]:
    low, controls, trade_totals = select_liquidity_buckets(data, fold, low_count, control_count)
    btc = read_window(data / "BTCUSDT.parquet", fold.fit_start, fold.test_end)
    pairs: dict[str, pd.DataFrame] = {}
    for symbol in [*low, *controls]:
        pairs[symbol] = build_pair(read_window(data / f"{symbol}.parquet", fold.fit_start, fold.test_end), btc)

    fit: dict[str, pd.DataFrame] = {}
    validation: dict[str, pd.DataFrame] = {}
    train_full: dict[str, pd.DataFrame] = {}
    test: dict[str, pd.DataFrame] = {}
    validation_predictions: dict[str, dict[str, dict[float, np.ndarray]]] = {}
    for symbol in low:
        pair = pairs[symbol]
        fit[symbol] = pair.loc[pair.time.lt(fold.validation_start)].copy()
        validation[symbol] = pair.loc[pair.time.ge(fold.validation_start) & pair.time.lt(fold.test_start)].copy()
        train_full[symbol] = pair.loc[pair.time.lt(fold.test_start)].copy()
        test[symbol] = pair.loc[pair.time.ge(fold.test_start) & pair.time.lt(fold.test_end)].copy()
        validation_predictions[symbol] = {
            "entry": train_predictions(validation[symbol], fit[symbol], ENTRY_THRESHOLDS),
            "hold": train_predictions(validation[symbol], fit[symbol], HOLD_THRESHOLDS),
        }
    entry_threshold, hold_threshold, grid = choose_thresholds(validation_predictions, validation)

    validation_evidence: dict[str, dict[str, float | int]] = {}
    eligible: list[tuple[str, float]] = []
    for symbol, frame in validation.items():
        result = simulate_long(
            frame.target.to_numpy(),
            validation_predictions[symbol]["entry"][entry_threshold],
            validation_predictions[symbol]["hold"][hold_threshold],
            BASE_ONE_WAY_COST,
        )
        evidence = metrics(result.net, result.turnover)
        lag_correlation = float(fit[symbol].btc_return.corr(fit[symbol].target))
        validation_evidence[symbol] = {
            "lag_correlation": lag_correlation,
            "round_trips": int(evidence["round_trips"]),
            "net_pct_points": float(evidence["net_pct_points"]),
            "profit_factor": float(evidence["profit_factor"]),
        }
        # This is a train-only gate fixed before looking at the final week:
        # actual positive BTC lead and at least five cost-bearing validation trades.
        if (
            lag_correlation > 0
            and evidence["round_trips"] >= 5
            and evidence["net_pct_points"] > 0
        ):
            eligible.append((symbol, float(evidence["net_pct_points"])))
    qualified_symbols = [symbol for symbol, _ in sorted(eligible, key=lambda item: item[1], reverse=True)[:2]]

    symbol_reports: dict[str, Any] = {}
    low_parts: dict[str, list[pd.DataFrame]] = {"paper": [], "base": [], "stress": []}
    qualified_parts: dict[str, list[pd.DataFrame]] = {"paper": [], "base": [], "stress": []}
    control_parts: dict[str, list[pd.DataFrame]] = {"paper": [], "base": [], "stress": []}
    for symbol in [*low, *controls]:
        pair = pairs[symbol]
        train = pair.loc[pair.time.lt(fold.test_start)].copy()
        scoped_test = pair.loc[pair.time.ge(fold.test_start) & pair.time.lt(fold.test_end)].copy()
        entry_probability = predict_probability(fit_classifier(train, entry_threshold), scoped_test)
        hold_probability = predict_probability(fit_classifier(train, hold_threshold), scoped_test)
        report: dict[str, Any] = {}
        outputs: dict[str, pd.DataFrame] = {}
        for name, cost in (
            ("paper", PAPER_ONE_WAY_COST),
            ("base", BASE_ONE_WAY_COST),
            ("stress", STRESS_ONE_WAY_COST),
        ):
            result = simulate_long(scoped_test.target.to_numpy(), entry_probability, hold_probability, cost)
            report[name] = metrics(result.net, result.turnover)
            outputs[name] = pd.DataFrame(
                {
                    "open_time": scoped_test.open_time.to_numpy(),
                    name: result.net.to_numpy(),
                    "turnover": result.turnover.to_numpy(),
                }
            )
        symbol_reports[symbol] = report
        bucket = low_parts if symbol in low else control_parts
        for name in bucket:
            bucket[name].append(outputs[name])
            if symbol in qualified_symbols:
                qualified_parts[name].append(outputs[name])

    return {
        "month": fold.month,
        "low_liquidity": low,
        "controls": controls,
        "training_trade_count": {symbol: trade_totals[symbol] for symbol in [*low, *controls]},
        "selected_entry_threshold": entry_threshold,
        "selected_hold_threshold": hold_threshold,
        "validation_grid": grid,
        "validation_evidence": validation_evidence,
        "qualified_symbols": qualified_symbols,
        "symbols": symbol_reports,
        "low_portfolio": {name: aggregate_symbol_results(parts, name) for name, parts in low_parts.items()},
        "qualified_low_portfolio": {
            name: aggregate_symbol_results(parts, name) for name, parts in qualified_parts.items()
        },
        "control_portfolio": {name: aggregate_symbol_results(parts, name) for name, parts in control_parts.items()},
    }


def combine_fold_metrics(folds: list[dict[str, Any]], bucket: str, cost: str) -> dict[str, Any]:
    values = [fold[bucket][cost] for fold in folds]
    return {
        "folds": len(values),
        "positive_folds": sum(item["net_pct_points"] > 0 for item in values),
        "net_pct_points": float(sum(item["net_pct_points"] for item in values)),
        "round_trips": int(sum(item["round_trips"] for item in values)),
        "worst_fold_net_pct_points": float(min(item["net_pct_points"] for item in values)),
        "max_fold_drawdown_pct": float(min(item["max_drawdown_pct"] for item in values)),
    }


def main() -> None:
    args = parse_args()
    months = [str(period) for period in pd.period_range(args.start, args.end, freq="M")]
    folds = [run_fold(args.data, fold_for_month(month), args.low_count, args.control_count) for month in months]
    summary = {
        bucket: {cost: combine_fold_metrics(folds, bucket, cost) for cost in ("paper", "base", "stress")}
        for bucket in ("low_portfolio", "qualified_low_portfolio", "control_portfolio")
    }
    low_stress = summary["qualified_low_portfolio"]["stress"]
    accepted = bool(
        low_stress["net_pct_points"] > 0
        and low_stress["positive_folds"] >= math.ceil(len(folds) * 0.67)
        and low_stress["round_trips"] >= 100
    )
    report = {
        "experiment": "s0_btc_alt_lead_lag",
        "source_method": "Kurihara-Matsumoto (2026) BTC-to-low-liquidity-alt lag proxy",
        "source_url": "https://doi.org/10.1007/s10690-026-09589-z",
        "execution": "features through closed minute t; fill next open; earn next open-to-open return",
        "universe": "mature USD-M contracts; low/control buckets ranked only on prior 21-day trade count",
        "threshold_selection": "days 1-14 fit; days 15-21 validation; days 22-month-end OOS",
        "costs": {
            "paper_one_way": PAPER_ONE_WAY_COST,
            "base_one_way": BASE_ONE_WAY_COST,
            "stress_one_way": STRESS_ONE_WAY_COST,
        },
        "folds": folds,
        "summary": summary,
        "accepted": accepted,
        "decision": "qualified_for_second_stage" if accepted else "rejected_no_live_or_shadow",
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": summary, "accepted": accepted}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
