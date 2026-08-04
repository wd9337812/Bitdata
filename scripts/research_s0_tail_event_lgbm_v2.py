from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

import joblib
import lightgbm as lgb
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
    "direction",
]
TRAIN_YEARS = {2020, 2021, 2022, 2023}
OOS_YEARS = (2024, 2025, 2026)
TOP_FRACTIONS = (0.01, 0.05)
RISK_PCT = 0.20
STOP_PCT = 0.12
START_EQUITY = 15.0
HARD_STOP = 5.0
LABEL_THRESHOLD_PCT = 24.5
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
                "direction": row.direction,
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
    parser.add_argument(
        "--use-trailing",
        action="store_true",
        help="Use 2R breakeven + 5R trailing-1.5R exit returns instead of fixed 36% TP.",
    )
    args = parser.parse_args()
    candidates = pd.read_parquet(args.candidates)
    candidates["year"] = pd.to_datetime(
        candidates.available_ms, unit="ms", utc=True
    ).dt.year
    candidates["entry_ms"] = candidates.available_ms + 3_600_000
    candidates["rank_720"] = candidates.groupby("available_ms")["ret_720h"].rank(
        pct=True
    )

    long_return_col = "long_trailing_return_pct" if args.use_trailing else "long_trade_return_pct"
    short_return_col = (
        "short_trailing_return_pct" if args.use_trailing else "short_trade_return_pct"
    )
    long_rows = candidates.assign(
        direction=1,
        label=(candidates.mfe_up_pct >= LABEL_THRESHOLD_PCT).astype(int),
        trade_return_pct=candidates[long_return_col],
    )
    short_rows = candidates.assign(
        direction=0,
        label=(candidates.mfe_down_pct >= LABEL_THRESHOLD_PCT).astype(int),
        trade_return_pct=candidates[short_return_col],
    )
    rows = pd.concat([long_rows, short_rows], ignore_index=True)
    for column in FEATURES:
        rows[column] = pd.to_numeric(rows[column], errors="coerce")
        rows[column] = rows[column].fillna(rows[column].median())

    train = rows.loc[rows.year.isin(TRAIN_YEARS)].copy()
    oos_rows = rows.loc[rows.year.isin(OOS_YEARS)].copy()
    model = lgb.LGBMClassifier(**LGB_PARAMS)
    model.fit(train[FEATURES], train["label"])
    oos_rows = oos_rows.copy()
    oos_rows["score"] = model.predict_proba(oos_rows[FEATURES])[:, 1]

    long_pred = oos_rows.loc[
        oos_rows.direction.eq(1), ["available_ms", "symbol", "score"]
    ].rename(columns={"score": "p_long"})
    short_pred = oos_rows.loc[
        oos_rows.direction.eq(0), ["available_ms", "symbol", "score"]
    ].rename(columns={"score": "p_short"})
    chosen = candidates.loc[candidates.year.isin(OOS_YEARS)].merge(
        long_pred, on=["available_ms", "symbol"], how="left"
    ).merge(short_pred, on=["available_ms", "symbol"], how="left")
    chosen["p_long"] = chosen.p_long.fillna(0.0)
    chosen["p_short"] = chosen.p_short.fillna(0.0)
    chosen["chosen_direction"] = chosen.apply(
        lambda r: "LONG" if r.p_long >= r.p_short else "SHORT",
        axis=1,
    )
    chosen["chosen_prob"] = chosen.apply(
        lambda r: max(r.p_long, r.p_short),
        axis=1,
    )
    chosen["trade_return_pct"] = chosen.apply(
        lambda r: (
            (r.long_trailing_return_pct if args.use_trailing else r.long_trade_return_pct)
            if r.chosen_direction == "LONG"
            else (
                r.short_trailing_return_pct
                if args.use_trailing
                else r.short_trade_return_pct
            )
        ),
        axis=1,
    )
    chosen["direction"] = chosen.chosen_direction

    args.output.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, args.output / "lgbm_v2.joblib")
    reports: dict[str, Any] = {}
    for year in OOS_YEARS:
        frame = chosen.loc[chosen.year.eq(year)]
        baseline_hit = float(
            (frame.mfe_max_pct >= LABEL_THRESHOLD_PCT).mean()
        )
        year_reports: dict[str, Any] = {
            "candidates": int(len(frame)),
            "baseline_hit_rate": round(baseline_hit, 6),
        }
        for fraction in TOP_FRACTIONS:
            top_n = max(1, int(len(frame) * fraction))
            top = frame.nlargest(top_n, "chosen_prob")
            chosen_hit = float((top.trade_return_pct > 0).mean())
            year_reports[f"top_{fraction:.0%}"] = {
                "n": int(len(top)),
                "chosen_win_rate": round(chosen_hit, 6),
                "equity": simulate_equity(top),
            }
        reports[str(year)] = year_reports
        print(
            f"{year}: base_hit={baseline_hit:.3f} "
            + " ".join(
                f"top{f:.0%}_wr={year_reports[f'top_{f:.0%}']['chosen_win_rate']:.3f} "
                f"PF={year_reports[f'top_{f:.0%}']['equity']['profit_factor']:.2f} "
                f"final={year_reports[f'top_{f:.0%}']['equity']['final_equity']:.2f}U"
                for f in TOP_FRACTIONS
            ),
            flush=True,
        )

    combined = pd.concat(
        [
            frame.nlargest(max(1, int(len(frame) * 0.01)), "chosen_prob")
            for _, frame in chosen.groupby("year")
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
        "experiment": (
            "s0_tail_event_lgbm_v3_directional_trailing"
            if args.use_trailing
            else "s0_tail_event_lgbm_v2_directional"
        ),
        "features": FEATURES,
        "lgb_params": LGB_PARAMS,
        "train_years": sorted(TRAIN_YEARS),
        "oos_years": list(OOS_YEARS),
        "reports": reports,
        "combined_top1_equity": combined_equity,
        "combined_top1_without_top3": without_top3,
        "qualified": qualified,
        "feature_importance": dict(
            zip(FEATURES, [float(x) for x in model.feature_importances_])
        ),
        "warning": "Historical qualification is not live approval.",
    }
    report_name = "lgbm_v3_report.json" if args.use_trailing else "lgbm_v2_report.json"
    (args.output / report_name).write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
