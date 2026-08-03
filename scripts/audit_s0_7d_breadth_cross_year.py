from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_s0_adaptive_30d_momentum import (  # noqa: E402
    adjusted_cost,
    apply_event_time_gate,
    report,
)


DEFAULT_SOURCE = ROOT / "data" / "research" / "s0_7d_momentum_cross_year"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_7d_breadth_cross_year"
BREADTH_LOW = 0.015
BREADTH_HIGH = 0.075
BASE_COST_PCT = 0.36
STRESS_COST_PCT = 0.60
INDEPENDENT_END_MS = int(pd.Timestamp("2026-01-01", tz="UTC").timestamp() * 1000)


def attach_market_breadth(
    paths: pd.DataFrame,
    signals: pd.DataFrame,
) -> pd.DataFrame:
    signal_fields = signals[
        ["available_ms", "symbol", "direction", "market_breadth"]
    ].drop_duplicates(["available_ms", "symbol", "direction"], keep="last")
    merged = paths.merge(
        signal_fields,
        left_on=["signal_ms", "symbol", "direction"],
        right_on=["available_ms", "symbol", "direction"],
        how="left",
        validate="one_to_one",
    )
    if merged.market_breadth.isna().any():
        missing = int(merged.market_breadth.isna().sum())
        raise ValueError(f"{missing} path rows have no matching market breadth")
    return merged


def frozen_breadth_filter(paths: pd.DataFrame) -> pd.DataFrame:
    return paths.loc[
        paths.market_breadth.abs().between(BREADTH_LOW, BREADTH_HIGH)
    ].copy()


def scenario(paths: pd.DataFrame, cost_pct: float) -> dict[str, Any]:
    funded = apply_event_time_gate(adjusted_cost(paths, cost_pct))
    independent = funded.loc[funded.entry_ms < INDEPENDENT_END_MS].copy()
    context_2026 = funded.loc[funded.entry_ms >= INDEPENDENT_END_MS].copy()
    return {
        "all_years": report(funded),
        "independent_2021_2025": report(independent),
        "context_2026": report(context_2026),
    }


def qualifies(stress: dict[str, Any]) -> bool:
    independent = stress["independent_2021_2025"]
    overall = independent["overall"]
    blind = independent["cohorts"].get("blind", {})
    annual = independent["annual"]
    required_years = {"2021", "2022", "2023", "2024", "2025"}
    return bool(
        overall.get("trades", 0) >= 100
        and overall.get("profit_factor", 0) > 1.2
        and overall.get("net_pct_points", 0) > 0
        and required_years.issubset(annual)
        and all(
            annual[year].get("profit_factor", 0) > 1.0
            and annual[year].get("net_pct_points", 0) > 0
            for year in required_years
        )
        and blind.get("trades", 0) >= 15
        and blind.get("net_pct_points", 0) > 0
        and independent["without_top_3_symbols"].get("net_pct_points", 0) > 0
        and independent["bootstrap"].get("positive_probability", 0) >= 0.95
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Cross-year audit of the frozen 7d momentum rule with the "
            "1.5%-7.5% absolute market-breadth band."
        )
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    paths = pd.read_parquet(args.source / "independent_hourly_paths.parquet")
    signals = pd.read_parquet(args.source / "signals_all.parquet")
    filtered = frozen_breadth_filter(attach_market_breadth(paths, signals))
    scenarios = {
        f"cost_{cost:.2f}pct": scenario(filtered, cost)
        for cost in (BASE_COST_PCT, STRESS_COST_PCT)
    }
    stress = scenarios[f"cost_{STRESS_COST_PCT:.2f}pct"]
    qualified = qualifies(stress)
    result = {
        "experiment": "s0_7d_breadth_cross_year",
        "frozen_rule": {
            "formation_days": 7,
            "hold_days": 1,
            "stop_atr": 2.0,
            "reward_r": 2.0,
            "absolute_market_breadth": [BREADTH_LOW, BREADTH_HIGH],
            "selection_history": (
                "The breadth band was selected from 2026 research. "
                "Years 2021-2025 are the independent cross-year audit."
            ),
        },
        "filtered_paths": int(len(filtered)),
        "scenarios": scenarios,
        "qualified_for_minute_replay": qualified,
        "decision": "minute_replay_required" if qualified else "reject_before_minute_replay",
        "warning": "Historical qualification is not live-trading approval.",
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
