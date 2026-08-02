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
    MAX_EQUITY_RISK,
    MAX_LEVERAGE,
    load_hourly_btc,
    report,
    simulate_protection,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_market_tsmom_consensus_protection"
SLOW_LOOKBACK = 56
STOP_LOSSES = (0.05, 0.075, 0.10)


def selection_score(result: dict[str, Any]) -> tuple[float, float, float]:
    annual_returns = [
        float(values["net_return"]) for values in result["annual"].values()
    ]
    overall = result["overall"]
    return (
        min(annual_returns, default=-999.0),
        float(overall["profit_factor"]),
        float(overall["net_return"]),
    )


def qualifies_oos(result: dict[str, Any]) -> bool:
    annual = result["annual"]
    overall = result["overall"]
    required = {"2024", "2025", "2026"}
    return bool(
        required.issubset(annual)
        and overall["profit_factor"] > 1.10
        and overall["max_drawdown"] > -0.80
        and all(
            annual[year]["net_return"] > 0
            and annual[year]["profit_factor"] > 1.0
            for year in required
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate the fast-slow BTC trend candidate with hourly stop paths."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    panel = build_daily_panel(DEFAULT_DATA)
    state = consensus_state(market_state(panel), SLOW_LOOKBACK)
    signals = symbol_proxy_trades(panel, state, "BTCUSDT")
    hourly = load_hourly_btc(DEFAULT_DATA)
    candidates: dict[str, Any] = {}
    trades_by_label: dict[str, pd.DataFrame] = {}
    for stop_loss in STOP_LOSSES:
        label = f"stop_{stop_loss:.3f}"
        leverage = min(
            MAX_LEVERAGE,
            MAX_EQUITY_RISK / (stop_loss + 2.0 * STRESS_ONE_WAY_COST),
        )
        trades = simulate_protection(
            signals, hourly, None, stop_loss, STRESS_ONE_WAY_COST
        )
        years = pd.to_datetime(trades.entry_day, utc=True).dt.year
        trades_by_label[label] = trades
        candidates[label] = {
            "stop_loss_pct": stop_loss,
            "max_safe_leverage": round(leverage, 6),
            "training_stress": report(trades.loc[years.le(2023)], leverage),
            "oos_stress": report(trades.loc[years.ge(2024)], leverage),
            "full_stress": report(trades, leverage),
        }

    selected = max(
        candidates,
        key=lambda label: selection_score(candidates[label]["training_stress"]),
    )
    accepted = qualifies_oos(candidates[selected]["oos_stress"])
    trades_by_label[selected].to_parquet(
        args.output / "selected_stress_trades.parquet", index=False
    )
    result = {
        "experiment": "s0_market_tsmom_consensus_hourly_stop",
        "rule": "28-day trigger, positive 56-day trend, five-day hold, stop only",
        "selection": "choose fixed stop on 2020-2023 worst annual result, freeze 2024-2026",
        "selected": selected,
        "selected_result": candidates[selected],
        "accepted": accepted,
        "decision": "eligible_for_event_time_and_execution_validation"
        if accepted
        else "rejected_not_stable_positive_expectancy",
        "candidates": candidates,
    }
    (args.output / "report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {key: value for key, value in result.items() if key != "candidates"},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
