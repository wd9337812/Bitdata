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

from scripts.audit_s0_bnb_fixed_gate_hourly import (
    COSTS,
    CURRENT_THRESHOLD,
    DEFAULT_DATA,
    DELAYS,
    HARD_STOP,
    HARD_STOP_RESERVE,
    LEVERAGE,
    MARGIN_PCT,
    STARTING_EQUITY,
    STRESS_ONE_WAY_COST,
    build_daily_panel,
    executable_metrics,
    fixed_consensus_state,
    load_hourly_symbol,
    market_state,
    simulate_hourly_reentry,
)


DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_bnb_risk_budget"
CURRENT_RISK = 0.15
RISK_CANDIDATES = (CURRENT_RISK, 0.20, 0.25, 0.30)
STOP_PCT = 0.15
MAX_HOLD_HOURS = 5 * 24


def _slice_years(
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


def risk_period_report(
    trades: pd.DataFrame,
    *,
    one_way_cost: float,
    risk_pct: float,
) -> dict[str, Any]:
    years = pd.to_datetime(trades.entry_time, utc=True).dt.year

    def metrics(frame: pd.DataFrame) -> dict[str, Any]:
        return executable_metrics(
            frame,
            one_way_cost=one_way_cost,
            starting_equity=STARTING_EQUITY,
            risk_pct=risk_pct,
            leverage=LEVERAGE,
            margin_pct=MARGIN_PCT,
            hard_stop=HARD_STOP,
            reserve=HARD_STOP_RESERVE,
        )

    return {
        "overall": metrics(trades),
        "annual": {
            str(int(year)): metrics(group.drop(columns=["year"]))
            for year, group in trades.assign(year=years).groupby("year", sort=True)
        },
    }


def development_eligible(report: dict[str, Any]) -> bool:
    overall = report["overall"]
    annual = report["annual"]
    positive_years = sum(
        float(item["return_pct"]) > 0 and not item["hard_stopped"]
        for item in annual.values()
    )
    return bool(
        len(annual) >= 3
        and not overall["hard_stopped"]
        and float(overall["return_pct"]) > 0
        and float(overall["profit_factor"]) > 1.30
        and float(overall["max_drawdown_pct"]) > -60.0
        and positive_years >= 2
    )


def select_risk_budget(development: dict[float, dict[str, Any]]) -> float:
    eligible = [
        risk for risk, report in development.items() if development_eligible(report)
    ]
    if not eligible:
        return CURRENT_RISK
    return max(
        eligible,
        key=lambda risk: (
            float(development[risk]["overall"]["return_pct"]),
            float(development[risk]["overall"]["profit_factor"]),
            float(development[risk]["overall"]["max_drawdown_pct"]),
        ),
    )


def all_periods_positive(report: dict[str, Any]) -> bool:
    overall = report["overall"]
    annual = report["annual"]
    return bool(
        annual
        and not overall["hard_stopped"]
        and float(overall["return_pct"]) > 0
        and float(overall["profit_factor"]) > 1.0
        and all(
            float(item["return_pct"]) > 0
            and float(item["profit_factor"]) > 1.0
            and not item["hard_stopped"]
            for item in annual.values()
        )
    )


def promotion_accepted(
    *,
    selected_risk: float,
    current_oos: dict[str, Any],
    selected_validation: dict[str, Any],
    selected_stress: dict[str, dict[str, Any]],
) -> bool:
    selected_oos = selected_stress["0"]["stress_2x"]["overall"]
    return bool(
        selected_risk > CURRENT_RISK
        and all_periods_positive(selected_validation)
        and all(
            all_periods_positive(delay_report["stress_2x"])
            for delay_report in selected_stress.values()
        )
        and float(selected_oos["return_pct"]) > float(current_oos["return_pct"])
        and float(selected_oos["profit_factor"]) > 1.50
        and float(selected_oos["max_drawdown_pct"]) > -40.0
        and not selected_oos["hard_stopped"]
    )


def run_audit(panel: pd.DataFrame, hourly: pd.DataFrame) -> dict[str, Any]:
    state = fixed_consensus_state(
        market_state(panel),
        CURRENT_THRESHOLD,
    )
    trades_by_delay = {
        delay: simulate_hourly_reentry(
            hourly,
            state,
            execution_delay_hours=delay,
            stop_pct=STOP_PCT,
            max_hold_hours=MAX_HOLD_HOURS,
        )
        for delay in DELAYS
    }
    zero_delay = trades_by_delay[0]
    development = {
        risk: risk_period_report(
            _slice_years(zero_delay, None, 2022),
            one_way_cost=STRESS_ONE_WAY_COST,
            risk_pct=risk,
        )
        for risk in RISK_CANDIDATES
    }
    selected_risk = select_risk_budget(development)

    comparisons: dict[str, Any] = {}
    for label, risk in (("current", CURRENT_RISK), ("selected", selected_risk)):
        comparisons[label] = {
            "risk_pct": risk * 100,
            "development_2020_2022": development[risk],
            "validation_2023": risk_period_report(
                _slice_years(zero_delay, 2023, 2023),
                one_way_cost=STRESS_ONE_WAY_COST,
                risk_pct=risk,
            ),
            "oos_2024_plus_by_delay_and_cost": {
                str(delay): {
                    cost_label: risk_period_report(
                        _slice_years(trades, 2024, None),
                        one_way_cost=cost,
                        risk_pct=risk,
                    )
                    for cost_label, cost in COSTS.items()
                }
                for delay, trades in trades_by_delay.items()
            },
        }

    current_oos = comparisons["current"]["oos_2024_plus_by_delay_and_cost"]["0"][
        "stress_2x"
    ]["overall"]
    selected = comparisons["selected"]
    accepted = promotion_accepted(
        selected_risk=selected_risk,
        current_oos=current_oos,
        selected_validation=selected["validation_2023"],
        selected_stress=selected["oos_2024_plus_by_delay_and_cost"],
    )
    return {
        "experiment": "s0_bnb_fixed_gate_risk_budget",
        "rule": (
            "fixed 10.65% 28-day market gate plus positive 56-day market "
            "momentum; BNB long only; 15% exchange stop; five-day time exit"
        ),
        "selection": (
            "select risk only through 2022; require positive aggregate return, "
            "at least two of three positive years, PF>1.30, drawdown better than "
            "-60%, and no 5U hard stop; validate on 2023 and inspect 2024+ once"
        ),
        "execution": {
            "starting_equity": STARTING_EQUITY,
            "risk_candidates_pct": [risk * 100 for risk in RISK_CANDIDATES],
            "leverage": LEVERAGE,
            "margin_pct": MARGIN_PCT * 100,
            "hard_stop": HARD_STOP,
            "reserve": HARD_STOP_RESERVE,
            "stop_pct": STOP_PCT * 100,
            "max_hold_hours": MAX_HOLD_HOURS,
            "delays_hours": DELAYS,
            "one_way_costs": COSTS,
        },
        "development_candidates": {
            f"{risk * 100:.2f}": {
                "eligible": development_eligible(report),
                **report,
            }
            for risk, report in development.items()
        },
        "selected_risk_pct": selected_risk * 100,
        "comparisons": comparisons,
        "accepted_for_single_variable_promotion": accepted,
        "decision": (
            "promote_risk_budget_to_20_pct"
            if accepted and selected_risk == 0.20
            else "keep_current_risk_budget"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit BNB V5 account-risk candidates under executable sizing."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = run_audit(
        build_daily_panel(DEFAULT_DATA),
        load_hourly_symbol(DEFAULT_DATA),
    )
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "selected_risk_pct": report["selected_risk_pct"],
                "accepted": report["accepted_for_single_variable_promotion"],
                "decision": report["decision"],
                "comparisons": report["comparisons"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
