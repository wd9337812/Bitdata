from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from scripts.benchmark_s0_market_tsmom_28d import (
    DEFAULT_DATA,
    STRESS_ONE_WAY_COST,
    build_daily_panel,
    market_state,
    symbol_proxy_trades,
)
from scripts.benchmark_s0_market_tsmom_consensus import consensus_state
from scripts.benchmark_s0_market_tsmom_protection import (
    load_hourly_btc,
    report,
    simulate_protection,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_market_tsmom_execution_delay"
SLOW_LOOKBACK = 56
STOP_LOSS = 0.10
EQUITY_RISK = 0.15
DELAYS_HOURS = (0, 1, 2, 4)


def delay_signals(signals: pd.DataFrame, hours: int) -> pd.DataFrame:
    frame = signals.copy()
    delay = pd.Timedelta(hours=hours)
    frame["entry_day"] = pd.to_datetime(frame.entry_day, utc=True) + delay
    frame["exit_day"] = pd.to_datetime(frame.exit_day, utc=True) + delay
    return frame


def qualifies_scenarios(scenarios: dict[str, Any]) -> bool:
    required = {"2024", "2025", "2026"}
    for scenario in scenarios.values():
        report_ = scenario["oos_stress"]
        annual = report_["annual"]
        overall = report_["overall"]
        if not (
            required.issubset(annual)
            and overall["profit_factor"] > 1.10
            and overall["max_drawdown"] > -0.60
            and all(
                annual[year]["net_return"] > 0
                and annual[year]["profit_factor"] > 1.0
                for year in required
            )
        ):
            return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stress the consensus candidate against delayed execution."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    panel = build_daily_panel(DEFAULT_DATA)
    state = consensus_state(market_state(panel), SLOW_LOOKBACK)
    signals = symbol_proxy_trades(panel, state, "BTCUSDT")
    hourly = load_hourly_btc(DEFAULT_DATA)
    leverage = EQUITY_RISK / (STOP_LOSS + 2.0 * STRESS_ONE_WAY_COST)
    scenarios: dict[str, Any] = {}
    for hours in DELAYS_HOURS:
        label = f"delay_{hours}h"
        trades = simulate_protection(
            delay_signals(signals, hours),
            hourly,
            None,
            STOP_LOSS,
            STRESS_ONE_WAY_COST,
        )
        years = pd.to_datetime(trades.entry_day, utc=True).dt.year
        scenarios[label] = {
            "training_stress": report(trades.loc[years.le(2023)], leverage),
            "oos_stress": report(trades.loc[years.ge(2024)], leverage),
            "full_stress": report(trades, leverage),
        }
        trades.to_parquet(args.output / f"{label}_trades.parquet", index=False)

    accepted = qualifies_scenarios(scenarios)
    result = {
        "experiment": "s0_market_tsmom_execution_delay",
        "rule": "long-only 28/56-day consensus, five-day hold, 10% exchange stop",
        "equity_risk": EQUITY_RISK,
        "leverage": round(leverage, 6),
        "stress_one_way_cost": STRESS_ONE_WAY_COST,
        "scenarios": scenarios,
        "accepted": accepted,
        "decision": "eligible_for_shadow_integration"
        if accepted
        else "rejected_execution_timing_fragile",
    }
    (args.output / "report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
