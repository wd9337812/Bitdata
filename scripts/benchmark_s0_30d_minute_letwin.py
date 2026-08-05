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
from scripts.benchmark_s0_cross_sectional_momentum import (  # noqa: E402
    Profile,
    simulate_minute,
)

DEFAULT_SIGNALS = (
    ROOT / "data" / "research" / "s0_30d_momentum_adaptive_v2"
    / "signals_all.parquet"
)
DEFAULT_MINUTE = (
    ROOT / "data" / "research" / "binance_um_30d_all_event_1m" / "parquet"
)
DEFAULT_OUTPUT = (
    ROOT / "data" / "research" / "s0_30d_minute_letwin"
)
LAST_COMPLETE_SIGNAL_DATE = "2026-07-28"
DELAYS_MINUTES = (0, 5, 15)
COSTS_PCT = (0.36, 0.60)
SYMBOL_EMBARGO_HOURS = 72
REQUIRED_YEARS = {"2021", "2022", "2023", "2024", "2025", "2026"}


def profiles() -> tuple[Profile, ...]:
    """Pre-registered 'let winners run' profiles (selected on hourly dev evidence)."""
    return (
        Profile(
            "momentum_30d_letwin_3_5R",
            ("ret_720h",),
            0.05,
            2.5,
            3.5,
            168,
            12.0,
        ),
        Profile(
            "momentum_30d_letwin_4R",
            ("ret_720h",),
            0.05,
            2.5,
            4.0,
            168,
            12.0,
        ),
    )


def scenario_passes(result: dict[str, Any]) -> bool:
    overall = result.get("overall", {})
    annual = result.get("annual", {})
    diversified = result.get("without_top_3_symbols", {})
    blind = result.get("cohorts", {}).get("blind", {})
    bootstrap = result.get("bootstrap", {})
    return bool(
        REQUIRED_YEARS.issubset(annual)
        and all(
            annual[year].get("profit_factor", 0) > 1.0
            and annual[year].get("net_pct_points", 0) > 0
            for year in REQUIRED_YEARS
        )
        and overall.get("trades", 0) >= 60
        and overall.get("profit_factor", 0) > 1.2
        and diversified.get("profit_factor", 0) > 1.0
        and diversified.get("net_pct_points", 0) > 0
        and blind.get("trades", 0) >= 8
        and blind.get("profit_factor", 0) > 1.0
        and bootstrap.get("positive_probability", 0) >= 0.95
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Cross-year 1m-path audit of 30-day momentum with 2.5 ATR stop and "
            "3.5R/4R take-profit (let winners run, 7-day cap)."
        )
    )
    parser.add_argument("--signals", type=Path, default=DEFAULT_SIGNALS)
    parser.add_argument("--minute-dir", type=Path, default=DEFAULT_MINUTE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    signals = pd.read_parquet(args.signals)
    cutoff = pd.Timestamp(LAST_COMPLETE_SIGNAL_DATE, tz="UTC") + pd.Timedelta(days=1)
    signals = signals.loc[
        signals.available_ms.lt(int(cutoff.timestamp() * 1000))
    ].copy()
    results: dict[str, Any] = {}
    for profile in profiles():
        scenarios: dict[str, Any] = {}
        for delay in DELAYS_MINUTES:
            gross = simulate_minute(
                signals,
                args.minute_dir,
                profile,
                execution_delay_minutes=delay,
                cost_pct=0.0,
                enforce_single_position=False,
            )
            for cost in COSTS_PCT:
                paper = adjusted_cost(gross, cost)
                funded = apply_event_time_gate(
                    paper,
                    symbol_embargo_hours=SYMBOL_EMBARGO_HOURS,
                )
                name = f"delay_{delay}m_cost_{cost:.2f}pct"
                scenarios[name] = report(funded)
                funded.to_parquet(args.output / f"{profile.name}_{name}.parquet", index=False)
        results[profile.name] = {
            "profile": {
                "name": profile.name,
                "stop_atr": profile.stop_atr,
                "reward_r": profile.reward_r,
                "hold_hours": profile.hold_hours,
                "max_stop_pct": profile.max_stop_pct,
            },
            "scenarios": scenarios,
            "qualified_for_forward_shadow": all(
                scenario_passes(item) for item in scenarios.values()
            ),
        }
    report_document = {
        "experiment": "s0_30d_minute_letwin",
        "hypothesis": (
            "30-day cross-sectional selector with wider 2.5 ATR stop and "
            "3.5R/4R take-profit lets the family's right-skewed winners run; "
            "tested on official 1m event paths across 2021-2026."
        ),
        "frozen_before_evaluation": {
            "profiles": [
                {
                    "name": profile.name,
                    "stop_atr": profile.stop_atr,
                    "reward_r": profile.reward_r,
                    "hold_hours": profile.hold_hours,
                    "max_stop_pct": profile.max_stop_pct,
                }
                for profile in profiles()
            ],
            "execution_delays_minutes": list(DELAYS_MINUTES),
            "round_trip_costs_pct": list(COSTS_PCT),
            "symbol_embargo_hours": SYMBOL_EMBARGO_HOURS,
        },
        "signals": int(len(signals)),
        "profiles": results,
        "decision": (
            "qualified_requires_forward_shadow"
            if any(item["qualified_for_forward_shadow"] for item in results.values())
            else "reject_not_cross_year_robust"
        ),
    }
    (args.output / "report.json").write_text(
        json.dumps(report_document, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report_document, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
