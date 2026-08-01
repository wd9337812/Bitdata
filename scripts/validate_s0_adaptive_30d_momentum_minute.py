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
    adaptive_profile,
    apply_direction_gate,
    report,
)
from scripts.benchmark_s0_cross_sectional_momentum import simulate_minute  # noqa: E402
DEFAULT_RESEARCH = ROOT / "data" / "research" / "s0_30d_momentum_adaptive_v2"
DEFAULT_MINUTE = ROOT / "data" / "research" / "binance_um_30d_event_1m" / "parquet"
MIN_FUNDED_TRADES = 20
MIN_FUNDED_SYMBOLS = 8
MIN_BLIND_TRADES = 8


def minute_scenario_passes(scenario: dict[str, Any]) -> bool:
    funded = scenario.get("funded", {})
    overall = funded.get("overall", {})
    blind = funded.get("cohorts", {}).get("blind", {})
    diversified = funded.get("without_top_3_symbols", {})
    bootstrap = funded.get("bootstrap", {})
    return bool(
        overall.get("trades", 0) >= MIN_FUNDED_TRADES
        and overall.get("symbols", 0) >= MIN_FUNDED_SYMBOLS
        and overall.get("profit_factor", 0) > 1.2
        and overall.get("net_pct_points", 0) > 0
        and blind.get("trades", 0) >= MIN_BLIND_TRADES
        and blind.get("profit_factor", 0) > 1.0
        and blind.get("net_pct_points", 0) > 0
        and diversified.get("profit_factor", 0) > 1.0
        and diversified.get("net_pct_points", 0) > 0
        and bootstrap.get("positive_probability", 0) >= 0.95
    )


def initial_history(prior: pd.DataFrame) -> dict[str, list[float]]:
    years = pd.to_datetime(prior.entry_ms, unit="ms", utc=True).dt.year
    frame = prior.loc[years.lt(2026)].sort_values("entry_ms")
    return {
        direction: list(frame.loc[frame.direction.eq(direction), "net_pct"])
        for direction in ("LONG", "SHORT")
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Minute-path audit for adaptive 30d momentum.")
    parser.add_argument("--research", type=Path, default=DEFAULT_RESEARCH)
    parser.add_argument("--minute-dir", type=Path, default=DEFAULT_MINUTE)
    parser.add_argument("--last-complete-day", default="2026-07-31")
    args = parser.parse_args()
    signals = pd.read_parquet(args.research / "signals_2026.parquet")
    cutoff = pd.Timestamp(args.last_complete_day, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(days=5)
    signals = signals.loc[signals.available_ms.lt(int(cutoff.timestamp() * 1000))]
    prior = pd.read_parquet(args.research / "all_trades.parquet")
    history = initial_history(prior)
    scenarios: dict[str, Any] = {}
    for delay in (0, 5, 15):
        for cost in (0.36, 0.60):
            name = f"delay_{delay}m_cost_{cost:.2f}pct"
            paper = simulate_minute(
                signals,
                args.minute_dir,
                adaptive_profile(),
                execution_delay_minutes=delay,
                cost_pct=cost,
            )
            funded = apply_direction_gate(paper, history)
            scenarios[name] = {
                "paper": report(paper),
                "funded": report(funded),
            }
            funded.to_parquet(args.research / f"minute_funded_{delay}m_{cost:.2f}.parquet", index=False)
    robust_passed = all(minute_scenario_passes(scenario) for scenario in scenarios.values())
    research_shadow_eligible = bool(
        len(signals) >= 30
        and all(
            scenario["funded"]["overall"].get("net_pct_points", 0) > 0
            for scenario in scenarios.values()
        )
    )
    result = {
        "experiment": "s0_adaptive_30d_momentum_minute",
        "fully_closed_signals": int(len(signals)),
        "scenarios": scenarios,
        "robust_historical_minute_passed": robust_passed,
        "qualified_for_forward_shadow": robust_passed,
        "eligible_for_research_shadow": research_shadow_eligible,
        "decision": (
            "historical_minute_robustness_passed_requires_future_shadow"
            if robust_passed
            else "research_shadow_only_concentration_not_resolved"
        ),
        "warning": (
            "Research-shadow eligibility only means the candidate is worth collecting "
            "untouched future evidence. It is not live-trading approval."
        ),
    }
    (args.research / "minute_report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
