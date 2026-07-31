from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_s0_xmom_regime_candidates import (
    STRESS_COST,
    block_bootstrap,
    cost_adjusted,
)
from scripts.benchmark_s0_cross_sectional_momentum import summarize
from scripts.benchmark_s0_point_in_time_state_model import (
    DEFAULT_OUTPUT as STATIC_OUTPUT,
    FEATURE_COLUMNS,
    THRESHOLD_QUANTILES,
    add_model_features,
    choose_validation_threshold,
    eligible_rows,
    evaluate_threshold,
    purged_window,
    train_model,
)
from scripts.benchmark_s0_xmom_point_in_time import (
    DEFAULT_DATA,
    load_manifest,
)
from scripts.benchmark_s0_point_in_time_breakout import DEFAULT_PANEL


DEFAULT_OUTPUT = STATIC_OUTPUT.parent / "s0_point_in_time_state_model_walkforward"
WALK_FORWARD_CYCLES = (
    {
        "evaluation": "2026-04",
        "train": ("2026-01-01", "2026-03-01"),
        "calibration": ("2026-03-01", "2026-04-01"),
        "evaluate": ("2026-04-01", "2026-05-01"),
    },
    {
        "evaluation": "2026-05",
        "train": ("2026-01-01", "2026-04-01"),
        "calibration": ("2026-04-01", "2026-05-01"),
        "evaluate": ("2026-05-01", "2026-06-01"),
    },
    {
        "evaluation": "2026-06",
        "train": ("2026-01-01", "2026-05-01"),
        "calibration": ("2026-05-01", "2026-06-01"),
        "evaluate": ("2026-06-01", "2026-07-01"),
    },
    {
        "evaluation": "2026-07",
        "train": ("2026-01-01", "2026-06-01"),
        "calibration": ("2026-06-01", "2026-07-01"),
        "evaluate": ("2026-07-01", "2026-08-01"),
    },
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Monthly expanding walk-forward state model: train on earlier "
            "months, calibrate on the immediately preceding month, evaluate "
            "the next month without reading it."
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


def run_cycle(
    eligible: pd.DataFrame,
    panel: pd.DataFrame,
    cycle: dict[str, Any],
) -> tuple[dict[str, Any], pd.DataFrame]:
    train = purged_window(eligible, *cycle["train"])
    calibration = purged_window(eligible, *cycle["calibration"])
    evaluation = purged_window(eligible, *cycle["evaluate"])
    model = train_model(train)
    for frame in (train, calibration, evaluation):
        frame["predicted_return"] = model.predict(
            frame.loc[:, FEATURE_COLUMNS].astype("float32")
        )
    train_abs = np.abs(train.predicted_return.to_numpy())
    thresholds = {
        f"q{int(quantile * 1000):03d}": float(
            np.quantile(train_abs, quantile)
        )
        for quantile in THRESHOLD_QUANTILES
    }
    calibration_reports: dict[str, dict[str, Any]] = {}
    for name, threshold in thresholds.items():
        report, _ = evaluate_threshold(calibration, panel, threshold)
        calibration_reports[name] = report
    frozen_name = choose_validation_threshold(
        calibration_reports,
        minimum_trades=20,
    )
    if frozen_name is None:
        return {
            "evaluation_month": cycle["evaluation"],
            "train_window": cycle["train"],
            "calibration_window": cycle["calibration"],
            "evaluation_window": cycle["evaluate"],
            "train_rows": int(len(train)),
            "calibration_rows": int(len(calibration)),
            "evaluation_rows": int(len(evaluation)),
            "thresholds": thresholds,
            "calibration_reports": calibration_reports,
            "frozen_threshold_name": None,
            "evaluation": {
                "base": summarize(pd.DataFrame()),
                "stress": summarize(pd.DataFrame()),
            },
        }, pd.DataFrame()
    threshold = thresholds[frozen_name]
    _, trades = evaluate_threshold(evaluation, panel, threshold)
    trades["evaluation_month"] = cycle["evaluation"]
    stress = cost_adjusted(trades, STRESS_COST)
    return {
        "evaluation_month": cycle["evaluation"],
        "train_window": cycle["train"],
        "calibration_window": cycle["calibration"],
        "evaluation_window": cycle["evaluate"],
        "train_rows": int(len(train)),
        "calibration_rows": int(len(calibration)),
        "evaluation_rows": int(len(evaluation)),
        "thresholds": thresholds,
        "calibration_reports": calibration_reports,
        "frozen_threshold_name": frozen_name,
        "frozen_threshold": threshold,
        "evaluation": {
            "base": summarize(trades),
            "stress": summarize(stress),
        },
    }, trades


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
    cycles: list[dict[str, Any]] = []
    trade_parts: list[pd.DataFrame] = []
    for cycle in WALK_FORWARD_CYCLES:
        report, trades = run_cycle(eligible, panel, cycle)
        cycles.append(report)
        if not trades.empty:
            trade_parts.append(trades)
    combined = (
        pd.concat(trade_parts, ignore_index=True)
        if trade_parts
        else pd.DataFrame()
    )
    stressed = (
        cost_adjusted(combined, STRESS_COST)
        if not combined.empty
        else combined
    )
    july = (
        combined.loc[combined.evaluation_month.eq("2026-07")]
        if not combined.empty
        else combined
    )
    july_stress = (
        cost_adjusted(july, STRESS_COST) if not july.empty else july
    )
    profitable_months = sum(
        cycle["evaluation"]["base"].get("net_pct_points", 0) > 0
        for cycle in cycles
    )
    passed = bool(
        len(cycles) == 4
        and profitable_months >= 3
        and summarize(stressed).get("profit_factor", 0) > 1.05
        and summarize(july_stress).get("profit_factor", 0) > 1.0
    )
    report = {
        "method": (
            "Expanding monthly walk-forward. Each evaluation month uses a "
            "model trained only on earlier data and a threshold selected only "
            "from the immediately preceding month."
        ),
        "data": {
            "monthly_source": manifest.get("source"),
            "daily_extension": manifest.get("daily_extension"),
            "panel_rows": int(len(panel)),
            "eligible_rows": int(len(eligible)),
            "symbols": int(eligible.symbol.nunique()),
        },
        "cycles": cycles,
        "combined": {
            "base": summarize(combined),
            "stress": summarize(stressed),
            "profitable_months": profitable_months,
        },
        "final_july": {
            "base": summarize(july),
            "stress": summarize(july_stress),
            "weekly_block_bootstrap_base": block_bootstrap(july),
            "weekly_block_bootstrap_stress": block_bootstrap(july_stress),
        },
        "hourly_research_passed": passed,
        "live_qualified": False,
        "live_qualification_reason": (
            "Minute-level replay and independent shadow operation are required."
            if passed
            else "Walk-forward profitability or cost-stress gate failed."
        ),
    }
    if not combined.empty:
        combined.to_parquet(args.output / "trades.parquet", index=False)
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
