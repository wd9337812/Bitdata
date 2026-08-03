from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_s0_bnb_fixed_gate_hourly import (
    COSTS,
    CURRENT_THRESHOLD,
    DEFAULT_DATA,
    fixed_consensus_state,
    load_hourly_symbol,
    market_state,
    period_report,
    simulate_hourly_reentry,
)
from scripts.benchmark_s0_market_tsmom_28d import (
    STRESS_ONE_WAY_COST,
    build_daily_panel,
)


OUTPUT = ROOT / "data" / "research" / "s0_bnb_holding_frequency"
DEVELOPMENT_END_YEAR = 2022
VALIDATION_YEAR = 2023
DELAYS = (0, 1, 2, 4)


@dataclass(frozen=True)
class ExitProfile:
    name: str
    stop_pct: float
    max_hold_days: int


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


def slice_years(
    trades: pd.DataFrame,
    start: int | None,
    end: int | None,
) -> pd.DataFrame:
    years = pd.to_datetime(trades.entry_time, utc=True).dt.year
    mask = pd.Series(True, index=trades.index)
    if start is not None:
        mask &= years.ge(start)
    if end is not None:
        mask &= years.le(end)
    return trades.loc[mask].reset_index(drop=True)


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    panel = build_daily_panel(DEFAULT_DATA)
    hourly = load_hourly_symbol(DEFAULT_DATA)
    state = fixed_consensus_state(market_state(panel), CURRENT_THRESHOLD)
    candidates: dict[str, Any] = {}
    trades_by_name: dict[str, pd.DataFrame] = {}

    for profile in PROFILES:
        trades = simulate_hourly_reentry(
            hourly,
            state,
            execution_delay_hours=0,
            stop_pct=profile.stop_pct,
            max_hold_hours=profile.max_hold_days * 24,
        )
        candidates[profile.name] = {
            "parameters": asdict(profile),
            "development": period_report(
                slice_years(trades, None, DEVELOPMENT_END_YEAR),
                one_way_cost=STRESS_ONE_WAY_COST,
            ),
            "validation": period_report(
                slice_years(trades, VALIDATION_YEAR, VALIDATION_YEAR),
                one_way_cost=STRESS_ONE_WAY_COST,
            ),
            "oos": period_report(
                slice_years(trades, VALIDATION_YEAR + 1, None),
                one_way_cost=STRESS_ONE_WAY_COST,
            ),
        }
        trades_by_name[profile.name] = trades

    selected = max(
        candidates,
        key=lambda name: selection_score(candidates[name]["development"]),
    )
    selected_profile = next(profile for profile in PROFILES if profile.name == selected)
    selected_trades = trades_by_name[selected]
    selected_oos = slice_years(selected_trades, VALIDATION_YEAR + 1, None)
    selected_trades.to_parquet(OUTPUT / "selected_trades.parquet", index=False)

    delays: dict[str, Any] = {}
    for delay in DELAYS:
        trades = simulate_hourly_reentry(
            hourly,
            state,
            execution_delay_hours=delay,
            stop_pct=selected_profile.stop_pct,
            max_hold_hours=selected_profile.max_hold_days * 24,
        )
        delays[str(delay)] = period_report(
            slice_years(trades, VALIDATION_YEAR + 1, None),
            one_way_cost=STRESS_ONE_WAY_COST,
        )

    result = {
        "experiment": "s0_bnb_holding_frequency",
        "asset": "BNBUSDT",
        "market_gate": "frozen_28d_56d_consensus_threshold_10.65pct",
        "path_model": (
            "hourly stop path with next-daily-signal re-entry after an early exit"
        ),
        "selection_period": "2020-2022 development only",
        "validation_period": "2023",
        "out_of_sample_period": "2024+",
        "selection_rule": (
            "maximize positive development years, then worst annual return, "
            "then PF and total return"
        ),
        "stress_one_way_cost_pct": STRESS_ONE_WAY_COST * 100.0,
        "selected": selected,
        "selected_result": candidates[selected],
        "selected_oos_cost_stress": {
            label: period_report(selected_oos, one_way_cost=cost)
            for label, cost in COSTS.items()
        },
        "selected_oos_entry_delay_hours": delays,
        "candidates": candidates,
        "warning": (
            "The 2023 validation and 2024+ out-of-sample periods are not used "
            "to select the exit profile. Historical results do not guarantee profit."
        ),
    }
    (OUTPUT / "report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
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
