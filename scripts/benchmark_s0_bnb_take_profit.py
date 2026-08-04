from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_s0_bnb_fixed_gate_hourly import (  # noqa: E402
    COSTS,
    CURRENT_THRESHOLD,
    DEFAULT_DATA,
    fixed_consensus_state,
    load_hourly_symbol,
    period_report,
)
from scripts.audit_s0_bnb_fixed_gate_hourly import (  # noqa: E402
    HARD_STOP,
    HARD_STOP_RESERVE,
    LEVERAGE,
    MARGIN_PCT,
    MIN_NOTIONAL,
    MIN_QTY,
    QTY_STEP,
    STARTING_EQUITY,
    executable_metrics,
)
from scripts.benchmark_s0_market_tsmom_28d import (  # noqa: E402
    STRESS_ONE_WAY_COST,
    build_daily_panel,
    market_state,
)

DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_bnb_take_profit"
STOP_PCT = 0.15
MAX_HOLD_HOURS = 5 * 24


def simulate_bnb_tp(
    hourly: pd.DataFrame,
    state: pd.DataFrame,
    *,
    stop_pct: float = STOP_PCT,
    max_hold_hours: int = MAX_HOLD_HOURS,
    take_profit_r: float | None = None,
) -> pd.DataFrame:
    bars = hourly.sort_values("time").reset_index(drop=True).copy()
    times = pd.DatetimeIndex(bars.time)
    signal_days = pd.to_datetime(
        state.loc[state.signal.astype(bool), "day"], utc=True
    ).sort_values()
    rows: list[dict[str, Any]] = []
    next_available = pd.Timestamp.min.tz_localize("UTC")

    for signal_day in signal_days:
        planned_entry = signal_day + pd.Timedelta(days=1)
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

        stop_price = entry_price * (1.0 - float(stop_pct))
        take_price = (
            entry_price * (1.0 + float(stop_pct) * float(take_profit_r))
            if take_profit_r is not None
            else None
        )
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
            if bar_open <= stop_price:
                exit_index = path_index
                exit_time = pd.Timestamp(bar.time)
                exit_price = bar_open
                exit_reason = "gap_stop"
                break
            if float(bar.low) <= stop_price:
                exit_index = path_index
                exit_time = pd.Timestamp(bar.time)
                exit_price = stop_price
                exit_reason = "stop"
                break
            if take_price is not None and bar_open >= take_price:
                exit_index = path_index
                exit_time = pd.Timestamp(bar.time)
                exit_price = bar_open
                exit_reason = "gap_take"
                break
            if take_price is not None and float(bar.high) >= take_price:
                exit_index = path_index
                exit_time = pd.Timestamp(bar.time)
                exit_price = take_price
                exit_reason = "take"
                break

        rows.append(
            {
                "signal_day": signal_day,
                "entry_time": entry_time,
                "exit_time": exit_time,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "initial_stop_pct": float(stop_pct),
                "direction": 1,
                "gross_return": exit_price / entry_price - 1.0,
                "exit_reason": exit_reason,
                "hold_hours": max(
                    0.0, (exit_time - entry_time).total_seconds() / 3600.0
                ),
            }
        )
        next_available = exit_time
    return pd.DataFrame(rows)


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
    DEFAULT_OUTPUT.mkdir(parents=True, exist_ok=True)
    panel = build_daily_panel(DEFAULT_DATA)
    hourly = load_hourly_symbol(DEFAULT_DATA)
    state = fixed_consensus_state(market_state(panel), CURRENT_THRESHOLD)
    reports: dict[str, Any] = {}
    for label, tp_r in (
        ("no_tp", None),
        ("tp_1r", 1.0),
        ("tp_1_5r", 1.5),
        ("tp_2r", 2.0),
        ("tp_3r", 3.0),
    ):
        trades = simulate_bnb_tp(
            hourly,
            state,
            stop_pct=STOP_PCT,
            max_hold_hours=MAX_HOLD_HOURS,
            take_profit_r=tp_r,
        )
        reports[label] = {
            "trades": int(len(trades)),
            "development": period_report(
                slice_years(trades, None, 2022),
                one_way_cost=STRESS_ONE_WAY_COST,
            ),
            "validation": period_report(
                slice_years(trades, 2023, 2023),
                one_way_cost=STRESS_ONE_WAY_COST,
            ),
            "oos": period_report(
                slice_years(trades, 2024, None),
                one_way_cost=STRESS_ONE_WAY_COST,
            ),
            "oos_equity_20pct": executable_metrics(
                slice_years(trades, 2024, None),
                one_way_cost=STRESS_ONE_WAY_COST,
                risk_pct=0.20,
            ),
        }
        o = reports[label]["oos"]["overall"]
        e = reports[label]["oos_equity_20pct"]
        print(
            f"{label}: oos_trades={o.get('trades')} oos_PF={o.get('profit_factor')} "
            f"oos_ret={o.get('return_pct')}% equity20={e['ending_equity']}U "
            f"hard={e['hard_stopped']}",
            flush=True,
        )
    result = {
        "experiment": "s0_bnb_take_profit",
        "asset": "BNBUSDT",
        "profile": {"stop_pct": STOP_PCT, "max_hold_hours": MAX_HOLD_HOURS},
        "cost_stress": COSTS,
        "reports": reports,
        "warning": "Historical qualification is not live-trading approval.",
    }
    (DEFAULT_OUTPUT / "report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
