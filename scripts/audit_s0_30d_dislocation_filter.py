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
DEFAULT_PREMIUM = (
    ROOT / "data" / "research" / "s0_30d_bybit_premium"
    / "signal_premiums.parquet"
)
DEFAULT_MINUTE = ROOT / "data" / "research" / "binance_um_1m_cross_year" / "parquet"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_30d_dislocation_filter"

PROFILES = (
    Profile("frozen_2_5R", ("ret_720h",), 0.05, 2.5, 2.5, 120, 12.0),
    Profile("letwin_3_5R", ("ret_720h",), 0.05, 2.5, 3.5, 168, 12.0),
    Profile("letwin_4R", ("ret_720h",), 0.05, 2.5, 4.0, 168, 12.0),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Test Binance-Bybit dislocation filters on 30d momentum signals "
            "(registration-free Bybit public data)."
        )
    )
    parser.add_argument("--signals", type=Path, default=DEFAULT_SIGNALS)
    parser.add_argument("--premium", type=Path, default=DEFAULT_PREMIUM)
    parser.add_argument("--minute-dir", type=Path, default=DEFAULT_MINUTE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--delay", type=int, default=5)
    return parser.parse_args()


def apply_filter(
    signals: pd.DataFrame,
    premium: pd.DataFrame,
    name: str,
    abs_threshold: float | None,
    directional_threshold: float | None,
    delay_minutes: int = 5,
) -> pd.DataFrame:
    merged = signals.merge(
        premium[premium.delay_minutes.eq(delay_minutes)][
            ["symbol", "available_ms", "premium_pct"]
        ],
        on=["symbol", "available_ms"],
        how="inner",
    )
    if name == "baseline":
        return signals
    if abs_threshold is not None:
        merged = merged[merged.premium_pct.abs() <= abs_threshold]
    if directional_threshold is not None:
        long_ok = (merged.direction.eq("LONG")) & (
            merged.premium_pct <= directional_threshold
        )
        short_ok = (merged.direction.eq("SHORT")) & (
            merged.premium_pct >= -directional_threshold
        )
        merged = merged[long_ok | short_ok]
    return merged[signals.columns]


FILTERS = (
    ("baseline", None, None),
    ("abs<=0.10", 0.10, None),
    ("abs<=0.20", 0.20, None),
    ("abs<=0.50", 0.50, None),
    ("dir<=0.10", None, 0.10),
    ("dir<=0.05", None, 0.05),
)


def main() -> None:
    args = parse_args()
    signals = pd.read_parquet(args.signals)
    premium = pd.read_parquet(args.premium)
    args.output.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}
    for profile in PROFILES:
        profile_results: dict[str, Any] = {}
        for filter_name, abs_thr, dir_thr in FILTERS:
            filtered = apply_filter(
                signals,
                premium,
                filter_name,
                abs_thr,
                dir_thr,
                args.delay,
            )
            gross = simulate_minute(
                filtered,
                args.minute_dir,
                profile,
                execution_delay_minutes=args.delay,
                cost_pct=0.0,
                enforce_single_position=False,
            )
            scenarios: dict[str, Any] = {}
            for cost in (0.36, 0.60):
                paper = adjusted_cost(gross, cost)
                funded = apply_event_time_gate(paper, symbol_embargo_hours=72)
                scenarios[f"cost_{cost:.2f}"] = report(funded)
            profile_results[filter_name] = {
                "signals": int(len(filtered)),
                "scenarios": scenarios,
            }
        results[profile.name] = profile_results
    document = {
        "experiment": "s0_30d_dislocation_filter",
        "hypothesis": (
            "Avoid 30d momentum entries when Binance-Bybit dislocation is extreme "
            "(crowded entry); Bybit data is registration-free public archive."
        ),
        "execution_delay_minutes": args.delay,
        "filters": [item[0] for item in FILTERS],
        "results": results,
    }
    (args.output / "report.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # Print compact comparison table.
    for profile_name, profile_results in results.items():
        print(f"=== {profile_name} ===")
        for filter_name, item in profile_results.items():
            overall = item["scenarios"]["cost_0.60"]["overall"]
            annual = item["scenarios"]["cost_0.60"]["annual"]
            without = item["scenarios"]["cost_0.60"]["without_top_3_symbols"]
            boot = item["scenarios"]["cost_0.60"]["bootstrap"]
            year_pf = ";".join(
                f"{year}:{annual[year]['profit_factor']:.2f}"
                for year in sorted(annual)
            )
            print(
                f"{filter_name:12s} signals={item['signals']:3d} "
                f"trades={overall.get('trades', 0):3d} "
                f"PF={overall.get('profit_factor', 0):.2f} "
                f"net={overall.get('net_pct_points', 0):7.1f} "
                f"woTop3PF={without.get('profit_factor', 0):.2f} "
                f"boot={boot.get('positive_probability', 0):.3f} | {year_pf}"
            )


if __name__ == "__main__":
    main()
