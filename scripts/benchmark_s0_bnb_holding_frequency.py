from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_s0_executable_major_rotation import (
    ExitProfile,
    bootstrap_net_return,
    build_features,
    executable_metrics,
    period_report,
    simulate_rotation,
)
from scripts.benchmark_s0_market_tsmom_28d import (
    DEFAULT_DATA,
    STRESS_ONE_WAY_COST,
    build_daily_panel,
    market_state,
)
from scripts.benchmark_s0_market_tsmom_consensus import consensus_state


OUTPUT = ROOT / "data" / "research" / "s0_bnb_holding_frequency"
TRAIN_END_YEAR = 2023
PROFILES = tuple(
    ExitProfile(
        f"time{hold_days}_stop{stop_pct:g}",
        stop_pct / 100.0,
        hold_days,
    )
    for hold_days in (1, 2, 3, 5)
    for stop_pct in (5.0, 7.5, 10.0, 15.0)
)


def selection_score(report: dict[str, Any]) -> tuple[int, float, float, float]:
    annual_returns = [
        float(item["return_pct"])
        for item in report["annual"].values()
    ]
    return (
        sum(value > 0 for value in annual_returns),
        min(annual_returns, default=-999.0),
        float(report["overall"]["profit_factor"]),
        float(report["overall"]["return_pct"]),
    )


def cost_stress_report(
    trades: pd.DataFrame,
    *,
    starting_equity: float = 15.0,
    risk_pct: float = 0.15,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for multiplier in (1.0, 1.5, 2.0):
        stressed = trades.copy()
        stressed["net_return"] = stressed.net_return - (
            2.0 * STRESS_ONE_WAY_COST * (multiplier - 1.0)
        )
        result[f"{multiplier:g}x"] = {
            "metrics": executable_metrics(
                stressed,
                starting_equity=starting_equity,
                risk_pct=risk_pct,
            ),
            "bootstrap": bootstrap_net_return(stressed),
        }
    return result


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    panel = build_daily_panel(DEFAULT_DATA)
    features = build_features(panel)
    state = consensus_state(market_state(panel), 56)
    candidates: dict[str, Any] = {}
    trades_by_name: dict[str, pd.DataFrame] = {}
    for profile in PROFILES:
        trades = simulate_rotation(
            panel,
            features,
            state,
            "fixed_BNBUSDT",
            profile,
        )
        years = pd.to_datetime(trades.entry_day, utc=True).dt.year
        train = trades.loc[years.le(TRAIN_END_YEAR)].copy()
        oos = trades.loc[years.gt(TRAIN_END_YEAR)].copy()
        candidates[profile.name] = {
            "train": period_report(train, 15.0, 0.15),
            "oos": period_report(oos, 15.0, 0.15),
        }
        trades_by_name[profile.name] = trades

    selected = max(
        candidates,
        key=lambda name: selection_score(candidates[name]["train"]),
    )
    selected_profile = next(profile for profile in PROFILES if profile.name == selected)
    selected_trades = trades_by_name[selected]
    selected_years = pd.to_datetime(selected_trades.entry_day, utc=True).dt.year
    selected_oos = selected_trades.loc[selected_years.gt(TRAIN_END_YEAR)].copy()
    selected_trades.to_parquet(OUTPUT / "selected_trades.parquet", index=False)

    delays: dict[str, Any] = {}
    for delay in (0, 1, 2):
        trades = simulate_rotation(
            panel,
            features,
            state,
            "fixed_BNBUSDT",
            selected_profile,
            delay_days=delay,
        )
        years = pd.to_datetime(trades.entry_day, utc=True).dt.year
        delays[str(delay)] = period_report(
            trades.loc[years.gt(TRAIN_END_YEAR)],
            15.0,
            0.15,
        )

    result = {
        "experiment": "s0_bnb_holding_frequency",
        "asset": "BNBUSDT",
        "market_gate": "frozen_28d_56d_consensus_threshold_10.65pct",
        "selection_period": "2020-2023 only",
        "selection_rule": (
            "maximize positive training years, then worst annual return, "
            "then PF and total return"
        ),
        "stress_one_way_cost_pct": STRESS_ONE_WAY_COST * 100.0,
        "selected": selected,
        "selected_result": candidates[selected],
        "selected_oos_cost_stress": cost_stress_report(selected_oos),
        "selected_oos_entry_delay_days": delays,
        "candidates": candidates,
        "warning": (
            "The 2024-2026 period is read once after the exit profile is selected. "
            "Historical results do not guarantee future profit."
        ),
    }
    (OUTPUT / "report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({key: value for key, value in result.items() if key != "candidates"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
