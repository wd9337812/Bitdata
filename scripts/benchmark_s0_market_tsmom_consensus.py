from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from scripts.benchmark_s0_market_tsmom_28d import (
    DEFAULT_DATA,
    HOLD_DAYS,
    STRESS_ONE_WAY_COST,
    build_daily_panel,
    evaluate,
    market_state,
    symbol_proxy_trades,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_market_tsmom_consensus"
SLOW_LOOKBACKS = (56, 84, 112)


def consensus_state(state: pd.DataFrame, slow_lookback: int) -> pd.DataFrame:
    frame = state.copy()
    frame["slow_momentum"] = frame.market_index.pct_change(slow_lookback)
    frame["signal"] = frame.signal & frame.slow_momentum.gt(0.0)
    return frame


def training_score(result: dict[str, Any]) -> tuple[float, float, float]:
    annual = result["annual"]
    annual_returns = [
        float(values["net_return"])
        for year, values in annual.items()
        if int(year) <= 2023
    ]
    overall = result["overall"]
    return (
        min(annual_returns, default=-999.0),
        float(overall["profit_factor"]),
        float(overall["net_return"]),
    )


def split_report(trades: pd.DataFrame) -> dict[str, Any]:
    years = pd.to_datetime(trades.entry_day, utc=True).dt.year
    training = trades.loc[years.le(2023)]
    oos = trades.loc[years.ge(2024)]
    return {
        "training_stress": evaluate(training, STRESS_ONE_WAY_COST),
        "oos_stress": evaluate(oos, STRESS_ONE_WAY_COST),
        "full_stress": evaluate(trades, STRESS_ONE_WAY_COST),
    }


def qualifies_oos(result: dict[str, Any]) -> bool:
    report = result["oos_stress"]
    annual = report["annual"]
    required = {"2024", "2025", "2026"}
    return bool(
        required.issubset(annual)
        and report["overall"]["profit_factor"] > 1.10
        and report["overall"]["max_drawdown"] > -0.60
        and all(
            annual[year]["net_return"] > 0
            and annual[year]["profit_factor"] > 1.0
            for year in required
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit 28-day market momentum with a slow trend confirmation."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    panel = build_daily_panel(DEFAULT_DATA)
    base_state = market_state(panel)
    candidates: dict[str, Any] = {}
    trades_by_label: dict[str, pd.DataFrame] = {}
    for lookback in SLOW_LOOKBACKS:
        label = f"slow_{lookback}d"
        trades = symbol_proxy_trades(
            panel, consensus_state(base_state, lookback), "BTCUSDT"
        )
        trades_by_label[label] = trades
        candidates[label] = split_report(trades)

    selected = max(
        candidates,
        key=lambda label: training_score(candidates[label]["training_stress"]),
    )
    accepted = qualifies_oos(candidates[selected])
    trades_by_label[selected].to_parquet(
        args.output / "selected_trades.parquet", index=False
    )
    result = {
        "experiment": "s0_market_tsmom_fast_slow_consensus",
        "rule": f"28-day market momentum trigger plus positive slow trend; hold {HOLD_DAYS} days",
        "selection": "choose slow lookback on 2020-2023 worst annual return, then freeze 2024-2026",
        "selected": selected,
        "selected_result": candidates[selected],
        "accepted": accepted,
        "decision": "eligible_for_hourly_path_validation"
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
