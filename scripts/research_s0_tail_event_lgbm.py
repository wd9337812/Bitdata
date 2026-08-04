from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_CANDIDATES = ROOT / "data" / "research" / "s0_tail_event_mfe" / "candidates.parquet"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_tail_event_mfe"
FEATURES = [
    "ret_24h",
    "ret_168h",
    "ret_720h",
    "atr_pct",
    "volume_shock",
    "funding_rate_pct",
    "rank_720",
]
TRAIN_YEARS = {2020, 2021, 2022, 2023}
OOS_YEARS = (2024, 2025, 2026)
TOP_FRACTIONS = (0.01, 0.05)
RISK_PCT = 0.20
STOP_PCT = 0.12
START_EQUITY = 15.0
HARD_STOP = 5.0
LGB_PARAMS = {
    "objective": "binary",
    "n_estimators": 300,
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_child_samples": 50,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "random_state": 20260805,
    "verbosity": -1,
}


def profit_factor(values: Iterable[float]) -> float:
    rows = [float(value) for value in values]
    gains = sum(value for value in rows if value > 0)
    losses = -sum(value for value in rows if value < 0)
    return gains / losses if losses > 0 else (999.0 if gains > 0 else 0.0)


def simulate_equity(
    trades: pd.DataFrame,
    *,
    start_equity: float = START_EQUITY,
    hard_stop: float = HARD_STOP,
) -> dict[str, Any]:
    ordered = trades.sort_values("entry_ms").reset_index(drop=True)
    equity = float(start_equity)
    peak = equity
    max_drawdown = 0.0
    stopped = False
    active_exit = -1
    selected: list[dict[str, Any]] = []
    for row in ordered.itertuples(index=False):
        if int(row.entry_ms) < active_exit:
            continue
        multiplier = 1.0 + float(row.trade_return_pct) / 100.0 * (RISK_PCT / STOP_PCT)
        equity *= multiplier
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak * 100 if peak > 0 else 0)
        selected.append(
            {
                "symbol": row.symbol,
                "entry_ms": int(row.entry_ms),
                "trade_return_pct": float(row.trade_return_pct),
            }
        )
        active_exit = int(row.entry_ms) + 120 * 3_600_000
        if equity <= hard_stop:
            stopped = True
            break
    frame = pd.DataFrame(selected)
    return {
        "trades": len(frame),
        "win_rate": round(float((frame.trade_return_pct > 0).mean()), 6) if len(frame) else 0.0,
        "profit_factor": round(profit_factor(frame.trade_return_pct), 6) if len(frame) else 0.0,
        "final_equity": round(equity, 6),
        "max_drawdown_pct": round(max_drawdown, 6),
        "hard_stop_hit": stopped,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    candidates = pd.read_parquet(args.candidates)
    candidates["year"] = pd.to_datetime(
        candidates.available_ms, unit="ms", utc=True
    ).dt.year
    candidates["entry_ms"] = candidates.available_ms + 3_600_000
    candidates["rank_720"] = candidates.groupby("available_ms")["ret_720h"].rank(
        pct=True
    )
    for column in FEATURES:
        candidates[column] = pd.to_numeric(candidates[column], errors="coerce")
        candidates[column] = candidates[column].fillna(candidates[column].median())

    train = candidates.loc[candidates.year.isin(TRAIN_YEARS)].copy()
    oos = candidates.loc[candidates.year.isin(OOS_YEARS)].copy()
    model = lgb.LGBMClassifier(**LGB_PARAMS)
    model.fit(train[FEATURES], train["label"])
    oos = oos.copy()
    oos["score"] = model.predict_proba(oos[FEATURES])[:, 1]
    args.output.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, args.output / "lgbm_v1.joblib")

    reports: dict[str, Any] = {}
    for year in OOS_YEARS:
        frame = oos.loc[oos.year.eq(year)]
        baseline_hit = float(frame.label.mean())
        year_reports: dict[str, Any] = {
            "candidates": int(len(frame)),
            "baseline_hit_rate": round(baseline_hit, 6),
        }
        for fraction in TOP_FRACTIONS:
            top_n = max(1, int(len(frame) * fraction))
            top = frame.nlargest(top_n, "score")
            year_reports[f"top_{fraction:.0%}"] = {
                "n": int(len(top)),
                "hit_rate": round(float(top.label.mean()), 6),
                "lift": round(float(top.label.mean() / max(baseline_hit, 1e-9)), 6),
                "equity": simulate_equity(top),
            }
        reports[str(year)] = year_reports
        print(
            f"{year}: base_hit={baseline_hit:.3f} "
            + " ".join(
                f"top{f:.0%}_hit={year_reports[f'top_{f:.0%}']['hit_rate']:.3f} "
                f"PF={year_reports[f'top_{f:.0%}']['equity']['profit_factor']:.2f} "
                f"final={year_reports[f'top_{f:.0%}']['equity']['final_equity']:.2f}U"
                for f in TOP_FRACTIONS
            ),
            flush=True,
        )

    combined = pd.concat(
        [
            frame.nlargest(max(1, int(len(frame) * 0.01)), "score")
            for _, frame in oos.groupby("year")
        ],
        ignore_index=True,
    )
    combined_equity = simulate_equity(combined)
    top_symbols = (
        combined.groupby("symbol").trade_return_pct.sum().nlargest(3).index
    )
    without_top3 = simulate_equity(
        combined.loc[~combined.symbol.isin(top_symbols)]
    )
    qualified = bool(
        combined_equity["trades"] >= 30
        and combined_equity["profit_factor"] > 1.2
        and not combined_equity["hard_stop_hit"]
        and all(
            reports[str(year)]["top_1%"]["equity"]["profit_factor"] > 1.0
            for year in OOS_YEARS
        )
        and without_top3["final_equity"] > START_EQUITY
    )
    result = {
        "experiment": "s0_tail_event_lgbm_v1",
        "features": FEATURES,
        "lgb_params": LGB_PARAMS,
        "train_years": sorted(TRAIN_YEARS),
        "oos_years": list(OOS_YEARS),
        "reports": reports,
        "combined_top1_equity": combined_equity,
        "combined_top1_without_top3": without_top3,
        "qualified": qualified,
        "feature_importance": dict(
            zip(
                FEATURES,
                [float(x) for x in model.feature_importances_],
            )
        ),
        "warning": "Historical qualification is not live approval.",
    }
    (args.output / "lgbm_report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
