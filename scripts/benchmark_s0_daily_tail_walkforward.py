from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier


BASE_ROUND_TRIP_COST = 0.0012
STRESS_ROUND_TRIP_COST = 0.0024
TAIL_RETURN = 0.08
MIN_CONFIDENCE = 0.34
MIN_24H_QUOTE_VOLUME = 20_000_000.0
MIN_AGE_DAYS = 60
HOLD_DAYS = 7
STOP_PROFILES = (0.10, 0.20, 0.30, 0.40)
EVALUATION_YEARS = (2023, 2024, 2025, 2026)
FEATURES = (
    "r1",
    "r3",
    "r7",
    "r14",
    "r30",
    "r60",
    "vol7",
    "vol30",
    "range",
    "qvr7",
    "qvr30",
    "age",
    "breadth7",
    "cs_r3",
    "cs_r7",
    "cs_r14",
    "cs_r30",
    "cs_r60",
    "cs_vol7",
    "cs_vol30",
    "cs_qvr7",
    "cs_qvr30",
    "cs_quote_volume",
    "btc_r1",
    "btc_r3",
    "btc_r7",
    "btc_r14",
    "btc_r30",
    "btc_r60",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--panel",
        type=Path,
        default=Path("data/research/s0_cross_sectional_reversal/daily_panel.parquet"),
    )
    parser.add_argument(
        "--history",
        type=Path,
        default=Path("data/research/binance_um_point_in_time_1h_2020_2023"),
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=Path("data/research/binance_um_point_in_time_1h_2024_2025"),
    )
    parser.add_argument(
        "--extension",
        type=Path,
        default=Path("data/research/binance_um_point_in_time_1h"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/research/s0_daily_tail_walkforward"),
    )
    return parser.parse_args()


def engineer_features(panel: pd.DataFrame) -> pd.DataFrame:
    panel = panel.sort_values(["symbol", "date"]).copy()
    grouped = panel.groupby("symbol", sort=False)
    panel["age"] = grouped.cumcount()
    for days in (1, 3, 7, 14, 30, 60):
        panel[f"r{days}"] = grouped.close.pct_change(days, fill_method=None)
    panel["vol7"] = (
        grouped.r1.rolling(7).std().reset_index(level=0, drop=True)
    )
    panel["vol30"] = (
        grouped.r1.rolling(30).std().reset_index(level=0, drop=True)
    )
    panel["range"] = (panel.high - panel.low) / panel.close
    panel["qvr7"] = panel.quote_volume / (
        grouped.quote_volume.rolling(7).mean().reset_index(level=0, drop=True)
    )
    panel["qvr30"] = panel.quote_volume / (
        grouped.quote_volume.rolling(30).mean().reset_index(level=0, drop=True)
    )
    panel["future_return"] = (
        grouped.open.shift(-(HOLD_DAYS + 1)) / grouped.open.shift(-1) - 1.0
    )
    future_date = grouped.date.shift(-(HOLD_DAYS + 1))
    panel.loc[
        future_date.sub(panel.date).ne(pd.Timedelta(days=HOLD_DAYS + 1)),
        "future_return",
    ] = np.nan
    for column in (
        "r3",
        "r7",
        "r14",
        "r30",
        "r60",
        "vol7",
        "vol30",
        "qvr7",
        "qvr30",
        "quote_volume",
    ):
        panel[f"cs_{column}"] = panel.groupby("date")[column].rank(pct=True)
    breadth = (
        panel.groupby("date").r7.apply(lambda values: float(values.gt(0).mean()))
    ).rename("breadth7")
    panel = panel.merge(breadth, on="date", how="left")
    btc = (
        panel.loc[panel.symbol.eq("BTCUSDT"), ["date", "r1", "r3", "r7", "r14", "r30", "r60"]]
        .set_index("date")
        .add_prefix("btc_")
    )
    panel = panel.merge(btc, left_on="date", right_index=True, how="left")
    panel["label"] = np.select(
        [panel.future_return.lt(-TAIL_RETURN), panel.future_return.gt(TAIL_RETURN)],
        [0, 2],
        default=1,
    )
    eligible = (
        panel.age.ge(MIN_AGE_DAYS)
        & panel.quote_volume.ge(MIN_24H_QUOTE_VOLUME)
        & panel.loc[:, FEATURES].notna().all(axis=1)
        & panel.future_return.notna()
    )
    return panel.loc[eligible].reset_index(drop=True)


def training_cutoff(year: int) -> pd.Timestamp:
    return pd.Timestamp(f"{year}-01-01", tz="UTC") - pd.Timedelta(
        days=HOLD_DAYS + 1
    )


def model() -> LGBMClassifier:
    return LGBMClassifier(
        objective="multiclass",
        n_estimators=300,
        learning_rate=0.03,
        num_leaves=15,
        max_depth=5,
        min_child_samples=200,
        subsample=0.8,
        colsample_bytree=0.8,
        reg_lambda=2.0,
        class_weight={0: 2.0, 1: 1.0, 2: 2.0},
        random_state=42,
        verbosity=-1,
        n_jobs=4,
    )


def select_single_position(scored: pd.DataFrame) -> pd.DataFrame:
    daily = (
        scored.sort_values(["date", "confidence"], ascending=[True, False])
        .drop_duplicates("date")
        .loc[lambda frame: frame.confidence.ge(MIN_CONFIDENCE)]
    )
    selected = []
    free_at = pd.Timestamp.min.tz_localize("UTC")
    for row in daily.itertuples():
        entry_time = row.date + pd.Timedelta(days=1)
        if entry_time >= free_at:
            selected.append(row.Index)
            free_at = entry_time + pd.Timedelta(days=HOLD_DAYS)
    return daily.loc[selected].sort_values("date").reset_index(drop=True)


def walk_forward(features: pd.DataFrame) -> pd.DataFrame:
    selected = []
    for year in EVALUATION_YEARS:
        train = features.loc[
            features.date.ge("2020-03-01")
            & features.date.lt(training_cutoff(year))
        ]
        current = features.loc[features.date.dt.year.eq(year)].copy()
        estimator = model()
        estimator.fit(train.loc[:, FEATURES], train.label)
        probabilities = estimator.predict_proba(current.loc[:, FEATURES])
        current["confidence"] = np.maximum(probabilities[:, 0], probabilities[:, 2])
        current["side"] = np.where(probabilities[:, 2] >= probabilities[:, 0], 1, -1)
        current["model_train_rows"] = len(train)
        current["model_train_cutoff"] = training_cutoff(year)
        chosen = select_single_position(current)
        chosen["evaluation_year"] = year
        selected.append(chosen)
    return pd.concat(selected, ignore_index=True)


def load_hourly(symbol: str, roots: list[Path]) -> pd.DataFrame:
    columns = ["open_time", "open", "high", "low", "close"]
    parts = []
    for root in roots:
        path = root / "parquet" / f"{symbol}.parquet"
        if path.exists():
            parts.append(pd.read_parquet(path, columns=columns))
    if not parts:
        return pd.DataFrame(columns=columns)
    return (
        pd.concat(parts, ignore_index=True)
        .sort_values("open_time")
        .drop_duplicates("open_time", keep="last")
        .reset_index(drop=True)
    )


def execute_stop_path(
    hourly: pd.DataFrame,
    signal_date: pd.Timestamp,
    side: int,
    stop: float,
) -> dict[str, object] | None:
    entry_ms = int((signal_date + pd.Timedelta(days=1)).timestamp() * 1000)
    exit_ms = int((signal_date + pd.Timedelta(days=HOLD_DAYS + 1)).timestamp() * 1000)
    path = hourly.loc[hourly.open_time.between(entry_ms, exit_ms)].copy()
    if path.empty or int(path.open_time.iloc[0]) != entry_ms:
        return None
    if int(path.open_time.iloc[-1]) != exit_ms:
        return None
    if path.open_time.diff().dropna().gt(3_600_000).any():
        return None
    entry = float(path.open.iloc[0])
    stop_price = entry * (1.0 - side * stop)
    exit_price = float(path.open.iloc[-1])
    exit_time = exit_ms
    exit_reason = "time"
    for row in path.iloc[:-1].itertuples(index=False):
        if side > 0:
            gap = row.open <= stop_price
            touched = row.low <= stop_price
        else:
            gap = row.open >= stop_price
            touched = row.high >= stop_price
        if gap:
            exit_price = float(row.open)
            exit_time = int(row.open_time)
            exit_reason = "stop_gap"
            break
        if touched:
            exit_price = stop_price
            exit_time = int(row.open_time)
            exit_reason = "stop"
            break
    return {
        "entry_time": entry_ms,
        "exit_time": exit_time,
        "entry": entry,
        "exit": exit_price,
        "exit_reason": exit_reason,
        "gross_return": side * (exit_price / entry - 1.0),
    }


def audit_paths(
    selected: pd.DataFrame, roots: list[Path]
) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    hourly_cache: dict[str, pd.DataFrame] = {}
    rows = []
    missing = []
    for trade in selected.itertuples(index=False):
        if trade.symbol not in hourly_cache:
            hourly_cache[trade.symbol] = load_hourly(trade.symbol, roots)
        for stop in STOP_PROFILES:
            result = execute_stop_path(
                hourly_cache[trade.symbol], trade.date, int(trade.side), stop
            )
            if result is None:
                missing.append(
                    {"symbol": trade.symbol, "date": trade.date.isoformat(), "stop": stop}
                )
                continue
            rows.append(
                {
                    "symbol": trade.symbol,
                    "signal_date": trade.date,
                    "evaluation_year": int(trade.evaluation_year),
                    "side": int(trade.side),
                    "confidence": float(trade.confidence),
                    "stop": stop,
                    **result,
                }
            )
    return pd.DataFrame(rows), missing


def metrics(returns: pd.Series) -> dict[str, float | int]:
    returns = returns.dropna()
    if returns.empty:
        return {
            "trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "net_return": 0.0,
            "mean_return": 0.0,
            "max_drawdown": 0.0,
        }
    gains = float(returns.loc[returns.gt(0)].sum())
    losses = float(-returns.loc[returns.lt(0)].sum())
    equity = returns.add(1.0).clip(lower=0.001).cumprod()
    drawdown = equity.div(equity.cummax()).sub(1.0)
    return {
        "trades": int(len(returns)),
        "win_rate": float(returns.gt(0).mean()),
        "profit_factor": gains / losses if losses else 999.0,
        "net_return": float(returns.sum()),
        "mean_return": float(returns.mean()),
        "max_drawdown": float(drawdown.min()),
    }


def summarize(trades: pd.DataFrame) -> dict[str, object]:
    result: dict[str, object] = {}
    for stop, scoped in trades.groupby("stop", sort=True):
        profile = {}
        for year in EVALUATION_YEARS:
            annual = scoped.loc[scoped.evaluation_year.eq(year)]
            profile[str(year)] = {
                "base_cost": metrics(annual.gross_return - BASE_ROUND_TRIP_COST),
                "stress_cost": metrics(annual.gross_return - STRESS_ROUND_TRIP_COST),
            }
        profile["combined"] = {
            "base_cost": metrics(scoped.gross_return - BASE_ROUND_TRIP_COST),
            "stress_cost": metrics(scoped.gross_return - STRESS_ROUND_TRIP_COST),
        }
        result[f"stop_{int(stop * 100)}pct"] = profile
    return result


def main() -> None:
    args = parse_args()
    roots = [args.history, args.data, args.extension]
    features = engineer_features(pd.read_parquet(args.panel))
    selected = walk_forward(features)
    trades, missing = audit_paths(selected, roots)
    results = summarize(trades)
    report = {
        "hypothesis": (
            "An expanding annual three-class model can rank one liquid perpetual whose "
            "next seven-day tail direction is more likely. A fixed 0.34 confidence gate "
            "and exchange-side hard stop bound the single-position path."
        ),
        "research_status": (
            "Rejected by exact hourly stop-path audit. Optimistic terminal-return "
            "clipping did not survive executable stop ordering, annual stability, "
            "winner-concentration, and timing-sensitivity checks."
        ),
        "features": list(FEATURES),
        "tail_return": TAIL_RETURN,
        "minimum_confidence": MIN_CONFIDENCE,
        "minimum_24h_quote_volume": MIN_24H_QUOTE_VOLUME,
        "selected_signals": int(len(selected)),
        "missing_paths": missing,
        "base_round_trip_cost": BASE_ROUND_TRIP_COST,
        "stress_round_trip_cost": STRESS_ROUND_TRIP_COST,
        "results": results,
        "live_qualified": False,
        "live_qualification_reason": (
            "The historical path is not robust across years and stop profiles. This "
            "candidate must not receive shadow takeover or live capital."
        ),
        "decision": "rejected_not_stable_positive_expectancy",
    }
    args.output.mkdir(parents=True, exist_ok=True)
    selected.to_parquet(args.output / "selected_signals.parquet", index=False)
    trades.to_parquet(args.output / "hourly_path_trades.parquet", index=False)
    (args.output / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
