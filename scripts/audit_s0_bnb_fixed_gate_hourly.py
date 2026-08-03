from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_s0_market_tsmom_28d import (
    DEFAULT_DATA,
    STRESS_ONE_WAY_COST,
    TOP_LIQUID_SYMBOLS,
    build_daily_panel,
    market_state,
)


DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_bnb_fixed_gate_hourly"
SYMBOL = "BNBUSDT"
CURRENT_THRESHOLD = 0.1065
THRESHOLDS = (0.075, 0.10, CURRENT_THRESHOLD, 0.125, 0.15, 0.20)
DELAYS = (0, 1, 2, 4)
COSTS = {
    "base_1x": STRESS_ONE_WAY_COST / 2,
    "stress_1_5x": STRESS_ONE_WAY_COST * 0.75,
    "stress_2x": STRESS_ONE_WAY_COST,
}
STARTING_EQUITY = 15.153
RISK_PCT = 0.15
LEVERAGE = 2.0
MARGIN_PCT = 0.90
HARD_STOP = 5.0
HARD_STOP_RESERVE = 0.50
STOP_PCT = 0.10
MAX_HOLD_HOURS = 5 * 24
MIN_QTY = 0.01
QTY_STEP = 0.01
MIN_NOTIONAL = 5.0


def fixed_consensus_state(
    state: pd.DataFrame,
    threshold: float,
    *,
    slow_lookback: int = 56,
) -> pd.DataFrame:
    frame = state.copy().sort_values("day").reset_index(drop=True)
    frame["slow_momentum"] = frame.market_index.pct_change(slow_lookback)
    frame["signal"] = (
        frame.universe_size.ge(TOP_LIQUID_SYMBOLS)
        & frame.momentum_28d.gt(float(threshold))
        & frame.slow_momentum.gt(0.0)
    )
    return frame


def load_hourly_symbol(
    data_dirs: tuple[Path, ...],
    symbol: str = SYMBOL,
) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for root in data_dirs:
        path = root / "parquet" / f"{symbol}.parquet"
        if path.exists():
            parts.append(
                pd.read_parquet(
                    path,
                    columns=["open_time", "open", "high", "low", "close"],
                )
            )
    if not parts:
        raise ValueError(f"No hourly archive found for {symbol}")
    frame = (
        pd.concat(parts, ignore_index=True)
        .sort_values("open_time")
        .drop_duplicates("open_time", keep="last")
        .reset_index(drop=True)
    )
    frame["time"] = pd.to_datetime(frame.open_time, unit="ms", utc=True)
    return frame


def simulate_hourly_reentry(
    hourly: pd.DataFrame,
    state: pd.DataFrame,
    *,
    execution_delay_hours: int,
    stop_pct: float = STOP_PCT,
    max_hold_hours: int = MAX_HOLD_HOURS,
    direction: int = 1,
) -> pd.DataFrame:
    if direction not in {-1, 1}:
        raise ValueError("direction must be -1 or 1")
    bars = hourly.sort_values("time").reset_index(drop=True).copy()
    times = pd.DatetimeIndex(bars.time)
    signal_days = pd.to_datetime(
        state.loc[state.signal.astype(bool), "day"], utc=True
    ).sort_values()
    rows: list[dict[str, Any]] = []
    next_available = pd.Timestamp.min.tz_localize("UTC")

    for signal_day in signal_days:
        planned_entry = signal_day + pd.Timedelta(
            days=1, hours=int(execution_delay_hours)
        )
        if planned_entry < next_available:
            continue
        entry_index = int(times.searchsorted(planned_entry, side="left"))
        if entry_index >= len(bars):
            continue
        entry = bars.iloc[entry_index]
        entry_time = pd.Timestamp(entry.time)
        if entry_time >= planned_entry + pd.Timedelta(hours=1):
            continue
        entry_price = float(entry.open)
        if not math.isfinite(entry_price) or entry_price <= 0:
            continue

        stop_price = entry_price * (1.0 - float(stop_pct) * direction)
        planned_exit = entry_time + pd.Timedelta(hours=int(max_hold_hours))
        exit_boundary = int(times.searchsorted(planned_exit, side="left"))
        if exit_boundary >= len(bars):
            continue

        exit_index = exit_boundary
        exit_time = pd.Timestamp(bars.iloc[exit_index].time)
        exit_price = float(bars.iloc[exit_index].open)
        exit_reason = "max_hold"
        for path_index in range(entry_index, exit_boundary):
            bar = bars.iloc[path_index]
            bar_open = float(bar.open)
            stop_crossed_at_open = (
                bar_open <= stop_price if direction > 0 else bar_open >= stop_price
            )
            stop_crossed_intrabar = (
                float(bar.low) <= stop_price
                if direction > 0
                else float(bar.high) >= stop_price
            )
            if stop_crossed_at_open:
                exit_index = path_index
                exit_time = pd.Timestamp(bar.time)
                exit_price = bar_open
                exit_reason = "gap_stop"
                break
            if stop_crossed_intrabar:
                exit_index = path_index
                exit_time = pd.Timestamp(bar.time)
                exit_price = stop_price
                exit_reason = "stop"
                break

        rows.append(
            {
                "signal_day": signal_day,
                "entry_time": entry_time,
                "exit_time": exit_time,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "initial_stop_pct": float(stop_pct),
                "direction": int(direction),
                "gross_return": (exit_price / entry_price - 1.0) * direction,
                "exit_reason": exit_reason,
                "hold_hours": max(
                    0.0, (exit_time - entry_time).total_seconds() / 3600.0
                ),
            }
        )
        next_available = exit_time
    return pd.DataFrame(rows)


def _floor_step(value: float, step: float) -> float:
    if value <= 0 or step <= 0:
        return 0.0
    return math.floor((value + 1e-12) / step) * step


def executable_metrics(
    trades: pd.DataFrame,
    *,
    one_way_cost: float,
    starting_equity: float = STARTING_EQUITY,
    risk_pct: float = RISK_PCT,
    leverage: float = LEVERAGE,
    margin_pct: float = MARGIN_PCT,
    hard_stop: float = HARD_STOP,
    reserve: float = HARD_STOP_RESERVE,
) -> dict[str, Any]:
    equity = float(starting_equity)
    peak = equity
    max_drawdown = 0.0
    rows: list[dict[str, Any]] = []
    skipped = 0

    for trade in trades.sort_values("entry_time").itertuples(index=False):
        if equity <= hard_stop:
            break
        entry_price = float(trade.entry_price)
        stop_distance = max(1e-9, float(trade.initial_stop_pct))
        risk_budget = min(
            equity * float(risk_pct),
            max(0.0, equity - float(hard_stop) - float(reserve)),
        )
        target_notional = min(
            risk_budget / stop_distance,
            equity * float(margin_pct) * float(leverage),
        )
        quantity = _floor_step(target_notional / entry_price, QTY_STEP)
        notional = quantity * entry_price
        if quantity < MIN_QTY or notional < MIN_NOTIONAL:
            skipped += 1
            continue

        gross_pnl = notional * float(trade.gross_return)
        cost = notional * 2.0 * float(one_way_cost)
        net_pnl = gross_pnl - cost
        equity += net_pnl
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1.0)
        rows.append(
            {
                "entry_time": trade.entry_time,
                "net_pnl": net_pnl,
                "gross_pnl": gross_pnl,
                "cost": cost,
            }
        )

    gross_profit = sum(max(0.0, float(row["net_pnl"])) for row in rows)
    gross_loss = sum(max(0.0, -float(row["net_pnl"])) for row in rows)
    gross_trade_profit = sum(float(row["gross_pnl"]) for row in rows)
    total_cost = sum(float(row["cost"]) for row in rows)
    return {
        "starting_equity": round(float(starting_equity), 6),
        "ending_equity": round(equity, 6),
        "return_pct": round((equity / float(starting_equity) - 1.0) * 100, 4),
        "executed": len(rows),
        "skipped_min_contract": skipped,
        "win_rate_pct": round(
            sum(float(row["net_pnl"]) > 0 for row in rows)
            / max(1, len(rows))
            * 100,
            4,
        ),
        "profit_factor": (
            round(gross_profit / gross_loss, 4)
            if gross_loss
            else (999.0 if gross_profit else 0.0)
        ),
        "max_drawdown_pct": round(max_drawdown * 100, 4),
        "gross_pnl_usdt": round(gross_trade_profit, 6),
        "cost_usdt": round(total_cost, 6),
        "cost_to_positive_gross_pct": round(
            total_cost / max(1e-9, sum(max(0.0, float(row["gross_pnl"])) for row in rows))
            * 100,
            4,
        ),
        "hard_stopped": equity <= hard_stop,
    }


def period_report(
    trades: pd.DataFrame,
    *,
    one_way_cost: float,
) -> dict[str, Any]:
    years = pd.to_datetime(trades.entry_time, utc=True).dt.year
    return {
        "overall": executable_metrics(trades, one_way_cost=one_way_cost),
        "annual": {
            str(int(year)): executable_metrics(
                group.drop(columns=["year"]),
                one_way_cost=one_way_cost,
            )
            for year, group in trades.assign(year=years).groupby("year", sort=True)
        },
    }


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


def _development_score(report: dict[str, Any]) -> tuple[float, float, float]:
    annual = report["annual"]
    return (
        min(
            (float(item["return_pct"]) for item in annual.values()),
            default=-999.0,
        ),
        float(report["overall"]["profit_factor"]),
        float(report["overall"]["return_pct"]),
    )


def _development_eligible(report: dict[str, Any]) -> bool:
    overall = report["overall"]
    annual = report["annual"]
    return bool(
        len(annual) >= 2
        and not overall["hard_stopped"]
        and float(overall["profit_factor"]) > 1.10
        and float(overall["max_drawdown_pct"]) > -50.0
        and all(
            float(item["return_pct"]) > 0 and not item["hard_stopped"]
            for item in annual.values()
        )
    )


def _all_positive(report: dict[str, Any]) -> bool:
    return bool(
        report["annual"]
        and not report["overall"]["hard_stopped"]
        and float(report["overall"]["profit_factor"]) > 1.0
        and all(
            float(item["return_pct"]) > 0
            and float(item["profit_factor"]) > 1.0
            and not item["hard_stopped"]
            for item in report["annual"].values()
        )
    )


def run_audit(
    panel: pd.DataFrame,
    hourly: pd.DataFrame,
) -> dict[str, Any]:
    base_state = market_state(panel)
    trades_by_threshold = {
        threshold: simulate_hourly_reentry(
            hourly,
            fixed_consensus_state(base_state, threshold),
            execution_delay_hours=0,
        )
        for threshold in THRESHOLDS
    }
    development = {
        threshold: period_report(
            _slice_years(trades, None, 2022),
            one_way_cost=STRESS_ONE_WAY_COST,
        )
        for threshold, trades in trades_by_threshold.items()
    }
    eligible = [
        threshold
        for threshold, report in development.items()
        if _development_eligible(report)
    ]
    selected_threshold = (
        max(eligible, key=lambda value: _development_score(development[value]))
        if eligible
        else CURRENT_THRESHOLD
    )

    comparisons: dict[str, Any] = {}
    for label, threshold in (
        ("current", CURRENT_THRESHOLD),
        ("development_selected", selected_threshold),
    ):
        delay_reports: dict[str, Any] = {}
        for delay in DELAYS:
            trades = simulate_hourly_reentry(
                hourly,
                fixed_consensus_state(base_state, threshold),
                execution_delay_hours=delay,
            )
            delay_reports[str(delay)] = {
                cost_label: period_report(
                    _slice_years(trades, 2024, None),
                    one_way_cost=cost,
                )
                for cost_label, cost in COSTS.items()
            }
        full_trades = trades_by_threshold[threshold]
        comparisons[label] = {
            "threshold_pct": threshold * 100,
            "development_2020_2022": development[threshold],
            "validation_2023": period_report(
                _slice_years(full_trades, 2023, 2023),
                one_way_cost=STRESS_ONE_WAY_COST,
            ),
            "oos_2024_plus_by_delay_and_cost": delay_reports,
        }

    selected = comparisons["development_selected"]
    current = comparisons["current"]
    selected_validation = selected["validation_2023"]
    selected_stress = selected["oos_2024_plus_by_delay_and_cost"]
    current_oos = current["oos_2024_plus_by_delay_and_cost"]["0"]["stress_2x"][
        "overall"
    ]
    selected_oos = selected_stress["0"]["stress_2x"]["overall"]
    robust = bool(
        selected_threshold != CURRENT_THRESHOLD
        and _all_positive(selected_validation)
        and all(
            _all_positive(delay_report["stress_2x"])
            for delay_report in selected_stress.values()
        )
        and float(selected_oos["profit_factor"])
        > float(current_oos["profit_factor"])
        and (
            float(selected_oos["return_pct"]) > float(current_oos["return_pct"])
            or float(selected_oos["max_drawdown_pct"])
            > float(current_oos["max_drawdown_pct"])
        )
    )
    return {
        "experiment": "s0_bnb_fixed_market_gate_hourly_reentry",
        "rule": (
            "fixed 28-day market momentum threshold plus positive 56-day market "
            "momentum; BNB long; next UTC-day hourly entry; 10% exchange stop; "
            "five-day time exit; next daily signal may re-enter after an early stop"
        ),
        "selection": (
            "select threshold only on data through 2022 with positive annual "
            "small-account results, PF>1.10, drawdown better than -50%, and no "
            "5U hard stop; validate on 2023; inspect 2024+ once"
        ),
        "execution": {
            "starting_equity": STARTING_EQUITY,
            "risk_pct": RISK_PCT * 100,
            "leverage": LEVERAGE,
            "margin_pct": MARGIN_PCT * 100,
            "hard_stop": HARD_STOP,
            "reserve": HARD_STOP_RESERVE,
            "stop_pct": STOP_PCT * 100,
            "max_hold_hours": MAX_HOLD_HOURS,
            "quantity_step": QTY_STEP,
            "minimum_notional": MIN_NOTIONAL,
            "delays_hours": DELAYS,
            "one_way_costs": COSTS,
        },
        "development_candidates": {
            f"{threshold * 100:.4f}": {
                "eligible": threshold in eligible,
                **report,
            }
            for threshold, report in development.items()
        },
        "selected_threshold_pct": selected_threshold * 100,
        "comparisons": comparisons,
        "accepted_for_single_variable_promotion": robust,
        "decision": (
            "promote_fixed_market_gate_only"
            if robust
            else "keep_current_gate_and_continue_forward_evidence"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit the live BNB fixed market gate with hourly re-entry."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    panel = build_daily_panel(DEFAULT_DATA)
    hourly = load_hourly_symbol(DEFAULT_DATA)
    report = run_audit(panel, hourly)
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "selected_threshold_pct": report["selected_threshold_pct"],
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
