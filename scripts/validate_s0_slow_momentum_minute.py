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

from scripts.audit_s0_xmom_regime_candidates import (  # noqa: E402
    BASE_COST,
    STRESS_COST,
    block_bootstrap,
    scope,
)
from scripts.benchmark_s0_cross_sectional_momentum import (  # noqa: E402
    simulate_minute,
    summarize,
)
from scripts.benchmark_s0_point_in_time_slow_momentum import (  # noqa: E402
    CANDIDATES,
    profile_for,
)


DEFAULT_RESEARCH = (
    ROOT / "data" / "research" / "s0_point_in_time_slow_momentum"
)
DEFAULT_MINUTE = (
    ROOT / "data" / "research" / "binance_um_event_1m" / "parquet"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the frozen slow-momentum candidate on official 1m "
            "execution paths, costs and entry delays."
        )
    )
    parser.add_argument("--research", type=Path, default=DEFAULT_RESEARCH)
    parser.add_argument("--minute-dir", type=Path, default=DEFAULT_MINUTE)
    return parser.parse_args()


def window_report(trades: pd.DataFrame) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, start, end in (
        ("development", "2026-02-01", "2026-04-01"),
        ("validation_apr_may", "2026-04-01", "2026-06-01"),
        ("test_june", "2026-06-01", "2026-07-01"),
        ("final_july", "2026-07-01", "2026-08-01"),
    ):
        selected = scope(trades, start, end)
        result[name] = {
            "metrics": summarize(selected),
            "bootstrap": block_bootstrap(selected),
        }
    return result


def qualifies(scenarios: dict[str, Any]) -> bool:
    required = (
        "validation_apr_may",
        "test_june",
        "final_july",
    )
    for scenario in scenarios.values():
        for window in required:
            metrics = scenario["windows"][window]["metrics"]
            if (
                metrics.get("trades", 0) < 20
                or metrics.get("profit_factor", 0) <= 1.0
                or metrics.get("net_pct_points", 0) <= 0
            ):
                return False
    return True


def main() -> None:
    args = parse_args()
    report = json.loads(
        (args.research / "report.json").read_text(encoding="utf-8")
    )
    selected_name = report.get("selected_candidate")
    if not selected_name:
        raise ValueError("Slow-momentum report has no frozen candidate")
    candidate = next(item for item in CANDIDATES if item.name == selected_name)
    signals = pd.read_parquet(args.research / "selected_signals.parquet")
    scenarios: dict[str, Any] = {}
    for delay in (0, 5, 15):
        for cost in (BASE_COST, STRESS_COST):
            name = f"delay_{delay}m_cost_{cost:.2f}pct"
            trades = simulate_minute(
                signals,
                args.minute_dir,
                profile_for(candidate),
                execution_delay_minutes=delay,
                cost_pct=cost,
            )
            scenarios[name] = {
                "delay_minutes": delay,
                "cost_pct": cost,
                "trades": int(len(trades)),
                "windows": window_report(trades),
            }
            trades.to_parquet(
                args.research / f"minute_trades_{delay}m_{cost:.2f}.parquet",
                index=False,
            )
    qualified = qualifies(scenarios)
    result = {
        "experiment": "s0_slow_momentum_minute_validation",
        "candidate": selected_name,
        "source": str(args.minute_dir),
        "signal_count": int(len(signals)),
        "scenarios": scenarios,
        "qualified_for_research_shadow": qualified,
        "decision": (
            "qualified_for_research_shadow"
            if qualified
            else "research_only_not_eligible"
        ),
    }
    (args.research / "minute_validation.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
