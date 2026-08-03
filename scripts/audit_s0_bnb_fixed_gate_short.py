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
    DELAYS,
    load_hourly_symbol,
    market_state,
    period_report,
    simulate_hourly_reentry,
)
from scripts.benchmark_s0_market_tsmom_28d import (
    STRESS_ONE_WAY_COST,
    TOP_LIQUID_SYMBOLS,
    build_daily_panel,
)


OUTPUT = ROOT / "data" / "research" / "s0_bnb_fixed_gate_short"
DEVELOPMENT_END_YEAR = 2022
VALIDATION_YEAR = 2023
THRESHOLDS = (0.075, 0.10, CURRENT_THRESHOLD, 0.125, 0.15, 0.20)


@dataclass(frozen=True)
class ShortProfile:
    name: str
    threshold: float
    stop_pct: float
    max_hold_days: int


PROFILES = tuple(
    ShortProfile(
        f"threshold{threshold:g}_time{hold_days}_stop{stop_pct:g}",
        threshold,
        stop_pct / 100.0,
        hold_days,
    )
    for threshold in THRESHOLDS
    for hold_days in (1, 2, 3, 5)
    for stop_pct in (5.0, 7.5, 10.0, 15.0)
)


def fixed_short_state(state: pd.DataFrame, threshold: float) -> pd.DataFrame:
    frame = state.copy().sort_values("day").reset_index(drop=True)
    frame["slow_momentum"] = frame.market_index.pct_change(56)
    frame["signal"] = (
        frame.universe_size.ge(TOP_LIQUID_SYMBOLS)
        & frame.momentum_28d.lt(-float(threshold))
        & frame.slow_momentum.lt(0.0)
    )
    return frame


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


def development_score(report: dict[str, Any]) -> tuple[int, float, float, float]:
    annual = [float(item["return_pct"]) for item in report["annual"].values()]
    return (
        sum(value > 0 for value in annual),
        min(annual, default=-999.0),
        float(report["overall"]["profit_factor"]),
        float(report["overall"]["return_pct"]),
    )


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    panel = build_daily_panel(DEFAULT_DATA)
    hourly = load_hourly_symbol(DEFAULT_DATA)
    base_state = market_state(panel)
    candidates: dict[str, Any] = {}
    trades_by_name: dict[str, pd.DataFrame] = {}

    for profile in PROFILES:
        trades = simulate_hourly_reentry(
            hourly,
            fixed_short_state(base_state, profile.threshold),
            execution_delay_hours=0,
            stop_pct=profile.stop_pct,
            max_hold_hours=profile.max_hold_days * 24,
            direction=-1,
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
        key=lambda name: development_score(candidates[name]["development"]),
    )
    profile = next(item for item in PROFILES if item.name == selected)
    selected_trades = trades_by_name[selected]
    selected_oos = slice_years(selected_trades, VALIDATION_YEAR + 1, None)
    delays = {
        str(delay): period_report(
            slice_years(
                simulate_hourly_reentry(
                    hourly,
                    fixed_short_state(base_state, profile.threshold),
                    execution_delay_hours=delay,
                    stop_pct=profile.stop_pct,
                    max_hold_hours=profile.max_hold_days * 24,
                    direction=-1,
                ),
                VALIDATION_YEAR + 1,
                None,
            ),
            one_way_cost=STRESS_ONE_WAY_COST,
        )
        for delay in DELAYS
    }
    result = {
        "experiment": "s0_bnb_fixed_gate_short",
        "selection_period": "2020-2022 development only",
        "validation_period": "2023",
        "out_of_sample_period": "2024+",
        "selected": selected,
        "selected_result": candidates[selected],
        "selected_oos_cost_stress": {
            label: period_report(selected_oos, one_way_cost=cost)
            for label, cost in COSTS.items()
        },
        "selected_oos_entry_delay_hours": delays,
        "candidates": candidates,
        "warning": (
            "This is an isolated audit. A selected profile is not eligible for "
            "live trading unless validation and each out-of-sample year remain "
            "positive after costs and delays."
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
