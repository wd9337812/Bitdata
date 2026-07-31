from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

try:
    from scripts import benchmark_s0_daily_order_flow as base
except ModuleNotFoundError:  # Direct script execution adds scripts/, not the repo root.
    import benchmark_s0_daily_order_flow as base


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PANEL = (
    ROOT / "data" / "research" / "s0_daily_order_flow" / "daily_order_flow_panel.parquet"
)
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_daily_order_flow_short_oof"
FOLDS = 4
STOP = 0.15
TARGET = 0.30


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Symbol-out-of-fold audit of the short-only daily order-flow proxy."
    )
    parser.add_argument("--panel", type=Path, default=DEFAULT_PANEL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def symbol_fold(symbol: str, folds: int = FOLDS) -> int:
    digest = hashlib.sha256(symbol.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % folds


def select_short_per_day(scored: pd.DataFrame) -> pd.DataFrame:
    candidates = scored.loc[scored.prediction.le(-base.STRESS_COST)].copy()
    candidates["side"] = -1
    candidates["expected_net"] = -candidates.prediction - base.STRESS_COST
    return (
        candidates.sort_values(
            ["date", "expected_net", "quote_volume"],
            ascending=[True, False, False],
        )
        .drop_duplicates("date")
        .sort_values("date")
        .reset_index(drop=True)
    )


def walk_forward_oof(features: pd.DataFrame) -> pd.DataFrame:
    data = features.copy()
    data["symbol_fold"] = data.symbol.map(symbol_fold)
    selected = []
    for year in base.EVALUATION_YEARS:
        start = pd.Timestamp(f"{year}-01-01", tz="UTC")
        predictions = []
        for fold in range(FOLDS):
            train = data.loc[
                data.exit_time.le(start)
                & data.date.ge("2020-03-01")
                & data.symbol_fold.ne(fold)
            ]
            current = data.loc[
                data.entry_time.dt.year.eq(year) & data.symbol_fold.eq(fold)
            ].copy()
            model = base.estimator()
            model.fit(train.loc[:, base.FEATURES], train.future_return)
            current["prediction"] = model.predict(current.loc[:, base.FEATURES])
            current["training_rows"] = len(train)
            current["training_cutoff"] = start
            predictions.append(current)
        chosen = select_short_per_day(pd.concat(predictions, ignore_index=True))
        chosen["evaluation_year"] = year
        selected.append(chosen)
    return pd.concat(selected, ignore_index=True)


def execute_path(
    trade: Any, hourly: pd.DataFrame, stop: float = STOP, target: float = TARGET
) -> dict[str, Any] | None:
    entry_ms = int(trade.entry_time.timestamp() * 1_000)
    exit_ms = int(trade.exit_time.timestamp() * 1_000)
    path = hourly.loc[hourly.open_time.between(entry_ms, exit_ms)].sort_values("open_time")
    if path.empty or int(path.open_time.iloc[0]) != entry_ms or int(path.open_time.iloc[-1]) != exit_ms:
        return None
    if path.open_time.diff().dropna().gt(3_600_000).any():
        return None
    entry = float(path.open.iloc[0])
    stop_price = entry * (1.0 + stop)
    target_price = entry * (1.0 - target)
    for row in path.iloc[:-1].itertuples(index=False):
        if row.open >= stop_price:
            return {"gross_return": 1.0 - float(row.open) / entry, "exit_reason": "stop_gap"}
        if row.high >= stop_price:
            return {"gross_return": -stop, "exit_reason": "stop"}
        if row.open <= target_price:
            return {"gross_return": 1.0 - float(row.open) / entry, "exit_reason": "target_gap"}
        if row.low <= target_price:
            return {"gross_return": target, "exit_reason": "target"}
    return {
        "gross_return": 1.0 - float(path.open.iloc[-1]) / entry,
        "exit_reason": "time",
    }


def audit_paths(selected: pd.DataFrame) -> tuple[pd.DataFrame, list[dict[str, str]]]:
    cache: dict[str, pd.DataFrame] = {}
    rows = []
    missing = []
    for trade in selected.itertuples(index=False):
        if trade.symbol not in cache:
            cache[trade.symbol] = base.load_hourly_symbol(trade.symbol)
        result = execute_path(trade, cache[trade.symbol])
        if result is None:
            missing.append({"symbol": trade.symbol, "entry_time": trade.entry_time.isoformat()})
            continue
        rows.append(
            {
                "symbol": trade.symbol,
                "symbol_fold": int(trade.symbol_fold),
                "signal_date": trade.date,
                "entry_time": trade.entry_time,
                "exit_time": trade.exit_time,
                "evaluation_year": int(trade.evaluation_year),
                "prediction": float(trade.prediction),
                **result,
            }
        )
    return pd.DataFrame(rows), missing


def summarize(trades: pd.DataFrame) -> dict[str, Any]:
    annual: dict[str, Any] = {}
    for year in base.EVALUATION_YEARS:
        scoped = trades.loc[trades.evaluation_year.eq(year)].copy()
        trimmed = scoped.drop(
            index=scoped.gross_return.nlargest(min(3, len(scoped))).index
        )
        annual[str(year)] = {
            "base": base.metric(scoped.gross_return - base.BASE_COST),
            "stress": base.metric(scoped.gross_return - base.STRESS_COST),
            "stress_without_top_three_winners": base.metric(
                trimmed.gross_return - base.STRESS_COST
            ),
        }
    folds = {
        str(fold): base.metric(
            scoped.gross_return - base.STRESS_COST
        )
        for fold, scoped in trades.groupby("symbol_fold", sort=True)
    }
    return {"annual": annual, "symbol_folds": folds}


def run(panel: pd.DataFrame) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame]:
    features = base.engineer_features(panel)
    selected = walk_forward_oof(features)
    trades, missing = audit_paths(selected)
    summary = summarize(trades)
    annual_pass = all(
        summary["annual"][str(year)]["stress"]["trades"] >= 30
        and summary["annual"][str(year)]["stress"]["profit_factor"] > 1.0
        and summary["annual"][str(year)]["stress"]["net_pct_points"] > 0.0
        and summary["annual"][str(year)]["stress_without_top_three_winners"]["profit_factor"] > 1.0
        for year in base.EVALUATION_YEARS
    )
    fold_values = list(summary["symbol_folds"].values())
    fold_pass = (
        sum(value["net_pct_points"] > 0.0 for value in fold_values) >= 3
        and min(value["profit_factor"] for value in fold_values) > 0.9
    )
    report = {
        "experiment": "s0_daily_order_flow_short_oof",
        "method": "Four deterministic symbol folds. Each symbol is scored by a nonlinear model that never trained on that symbol; the most negative daily forecast receives one short position with a 15% stop, 30% target, one-day timeout, and conservative stop-first hourly execution.",
        "discovery_note": "Short-only and the 15%/30% profile were selected after inspecting the preceding all-direction proxy. This experiment is a cross-symbol generalization audit, not untouched temporal blind validation.",
        "costs": {"base": base.BASE_COST, "stress": base.STRESS_COST},
        "selected_trades": int(len(selected)),
        "executed_paths": int(len(trades)),
        "missing_paths": missing,
        **summary,
        "decision": (
            "research_candidate_requires_fresh_forward_validation"
            if annual_pass and fold_pass and not missing
            else "rejected_not_positive_expectancy"
        ),
        "live_qualified": False,
    }
    return report, selected, trades


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    panel = pd.read_parquet(args.panel)
    report, selected, trades = run(panel)
    selected.to_parquet(args.output / "selected_signals.parquet", index=False)
    trades.to_parquet(args.output / "hourly_path_trades.parquet", index=False)
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"experiment": report["experiment"], "selected_trades": report["selected_trades"], "executed_paths": report["executed_paths"], "annual": report["annual"], "symbol_folds": report["symbol_folds"], "decision": report["decision"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
