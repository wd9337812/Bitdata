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

from scripts.benchmark_s0_donchian_ensemble import ONE_WAY_COST  # noqa: E402
from scripts.benchmark_s0_volume_weighted_tsmom import (  # noqa: E402
    STRESS_ONE_WAY_COST,
    build_daily_panel,
    daily_metrics,
    sparse_balanced_daily,
    weighted_portfolio_daily,
)


DEFAULT_DATA = ROOT / "data" / "research" / "binance_um_point_in_time_1h"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_volume_weighted_tsmom_2026"
LOOKBACK = 14


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate the frozen 14-day volume-weighted TSMOM family in 2026."
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def block_bootstrap_mean(
    returns: pd.Series,
    block_days: int = 7,
    iterations: int = 5_000,
    seed: int = 20260801,
) -> dict[str, float]:
    values = returns.dropna().to_numpy(dtype=float)
    if len(values) < block_days:
        return {"ci_low_daily_pct": 0.0, "ci_high_daily_pct": 0.0, "probability_mean_positive_pct": 0.0}
    rng = np.random.default_rng(seed)
    starts = np.arange(0, len(values) - block_days + 1)
    needed = int(np.ceil(len(values) / block_days))
    means = np.empty(iterations)
    for index in range(iterations):
        selected_starts = rng.choice(starts, size=needed, replace=True)
        sample = np.concatenate(
            [values[start : start + block_days] for start in selected_starts]
        )[: len(values)]
        means[index] = sample.mean()
    low, high = np.quantile(means, [0.025, 0.975])
    return {
        "ci_low_daily_pct": round(float(low * 100), 6),
        "ci_high_daily_pct": round(float(high * 100), 6),
        "probability_mean_positive_pct": round(float((means > 0).mean() * 100), 4),
    }


def assess(daily: pd.DataFrame) -> dict[str, Any]:
    windows = {
        "2026_jan_feb": ("2026-01-01", "2026-03-01"),
        "2026_mar_apr": ("2026-03-01", "2026-05-01"),
        "2026_may_jun": ("2026-05-01", "2026-07-01"),
        "2026_full": ("2026-01-01", "2026-07-01"),
    }
    report: dict[str, Any] = {}
    for name, (start, end) in windows.items():
        scoped = daily.loc[daily.day.ge(start) & daily.day.lt(end)].copy()
        report[name] = {
            **daily_metrics(scoped),
            "mean_daily_net_pct": round(float(scoped.net_return.mean() * 100), 6),
            "median_daily_net_pct": round(float(scoped.net_return.median() * 100), 6),
            "minimum_daily_net_pct": round(float(scoped.net_return.min() * 100), 6),
            "maximum_daily_net_pct": round(float(scoped.net_return.max() * 100), 6),
            "bootstrap": block_bootstrap_mean(scoped.net_return),
        }
    return report


def with_cost(daily: pd.DataFrame, one_way_cost: float) -> pd.DataFrame:
    result = daily.copy()
    result["net_return"] = result.gross_return - result.turnover * one_way_cost
    return result


def main() -> None:
    args = parse_args()
    panel, manifest = build_daily_panel(args.data)
    variants = {
        "frozen_top150_gross1": weighted_portfolio_daily(
            panel, LOOKBACK, ONE_WAY_COST, top_n=150, gross_exposure=1.0
        ),
        "sparse_1_per_side": sparse_balanced_daily(
            panel, LOOKBACK, 1, ONE_WAY_COST
        ),
        "sparse_3_per_side": sparse_balanced_daily(
            panel, LOOKBACK, 3, ONE_WAY_COST
        ),
        "sparse_5_per_side": sparse_balanced_daily(
            panel, LOOKBACK, 5, ONE_WAY_COST
        ),
    }
    results = {
        name: {
            "base": assess(daily),
            "stress": assess(with_cost(daily, STRESS_ONE_WAY_COST)),
        }
        for name, daily in variants.items()
    }
    frozen = results["frozen_top150_gross1"]
    qualifies = all(
        frozen[mode][window]["total_return_pct"] > 0
        and frozen[mode][window]["daily_profit_factor"] > 1.0
        for mode in ("base", "stress")
        for window in ("2026_jan_feb", "2026_mar_apr", "2026_may_jun")
    )
    report = {
        "experiment": "frozen_2026_volume_weighted_tsmom_validation",
        "source": manifest.get("source"),
        "frozen_before_2026_review": {
            "lookback_days": LOOKBACK,
            "top_n": 150,
            "gross_exposure": 1.0,
        },
        "results": results,
        "qualified_for_minute_validation": qualifies,
        "decision": "proceed_to_minute_validation" if qualifies else "reject_or_continue_research",
    }
    args.output.mkdir(parents=True, exist_ok=True)
    for name, daily in variants.items():
        daily.to_parquet(args.output / f"{name}_daily.parquet", index=False)
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
