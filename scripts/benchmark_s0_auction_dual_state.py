from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from benchmark_s0_volume_profile_tape import (
    BASE_ROUND_TRIP,
    DEFAULT_DATA,
    STRESS_ROUND_TRIP,
    apply_cost,
    load_funding,
    load_klines,
    metrics,
    prepare_sessions,
    profile_levels,
    remove_top_winners,
    window_metrics,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_auction_dual_state"
MAX_HOLD_BARS = 36


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit a preregistered balance-rejection / value-acceptance strategy."
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def value_area_overlap(first: dict[str, float], second: dict[str, float]) -> float:
    intersection = max(0.0, min(first["vah"], second["vah"]) - max(first["val"], second["val"]))
    narrower = min(first["vah"] - first["val"], second["vah"] - second["val"])
    return intersection / narrower if narrower > 0 else 0.0


def prepare_dual_state(raw: pd.DataFrame) -> pd.DataFrame:
    frame, _ = prepare_sessions(raw)
    counts = raw.groupby("day").size()
    complete_days = counts.loc[counts.eq(288)].index
    groups = {
        day: group.sort_values("time")
        for day, group in raw.loc[raw.day.isin(complete_days)].groupby("day", sort=True)
    }
    profiles = {day: profile_levels(group) for day, group in groups.items()}
    balance: dict[pd.Timestamp, bool] = {}
    overlap: dict[pd.Timestamp, float] = {}
    efficiency: dict[pd.Timestamp, float] = {}
    for day in frame.day.unique():
        previous_day = day - pd.Timedelta(days=1)
        second_previous_day = day - pd.Timedelta(days=2)
        if previous_day not in groups or second_previous_day not in groups:
            balance[day] = False
            overlap[day] = 0.0
            efficiency[day] = 1.0
            continue
        previous = groups[previous_day]
        day_range = float(previous.high.max() - previous.low.min())
        directional_efficiency = (
            abs(float(previous.close.iloc[-1] - previous.open.iloc[0])) / day_range
            if day_range > 0
            else 0.0
        )
        profile_overlap = value_area_overlap(profiles[previous_day], profiles[second_previous_day])
        efficiency[day] = directional_efficiency
        overlap[day] = profile_overlap
        balance[day] = profile_overlap >= 0.50 and directional_efficiency <= 0.35
    result = frame.copy()
    result["balanced_context"] = result.day.map(balance).fillna(False).astype(bool)
    result["value_area_overlap"] = result.day.map(overlap).fillna(0.0)
    result["prior_efficiency"] = result.day.map(efficiency).fillna(1.0)
    result["bar_index"] = result.groupby("day").cumcount()
    result["entry_open"] = result.groupby("day").open.shift(-1)
    result["rvol3"] = (
        result.groupby("day", group_keys=False)["rvol"]
        .rolling(3, min_periods=3)
        .mean()
        .reset_index(level=0, drop=True)
    )
    return result


def identify_candidates(frame: pd.DataFrame) -> pd.DataFrame:
    work = frame.copy()
    inside_profile = work.current_poc.between(work.prev_val, work.prev_vah)
    long_rejection = (
        work.balanced_context
        & work.low.le(work.prev_val * 0.9995)
        & work.close.gt(work.prev_val)
        & work.close.lt(work.prev_poc)
        & work.pulse.gt(0)
        & work.rvol.ge(1.2)
        & inside_profile
        & work.entry_open.lt(work.prev_poc)
    )
    short_rejection = (
        work.balanced_context
        & work.high.ge(work.prev_vah * 1.0005)
        & work.close.lt(work.prev_vah)
        & work.close.gt(work.prev_poc)
        & work.pulse.lt(0)
        & work.rvol.ge(1.2)
        & inside_profile
        & work.entry_open.gt(work.prev_poc)
    )

    above = work.close.gt(work.prev_vah)
    below = work.close.lt(work.prev_val)
    above3 = above & above.groupby(work.day).shift(1).fillna(False) & above.groupby(work.day).shift(2).fillna(False)
    below3 = below & below.groupby(work.day).shift(1).fillna(False) & below.groupby(work.day).shift(2).fillna(False)
    long_acceptance = (
        work.bar_index.ge(12)
        & above3
        & work.rvol3.ge(1.2)
        & work.pulse.gt(0)
        & work.current_poc.gt(work.prev_vah)
        & work.close.div(work.prev_vah).sub(1).between(0, 0.01)
    )
    short_acceptance = (
        work.bar_index.ge(12)
        & below3
        & work.rvol3.ge(1.2)
        & work.pulse.lt(0)
        & work.current_poc.lt(work.prev_val)
        & work.prev_val.div(work.close).sub(1).between(0, 0.01)
    )

    work["direction"] = np.select(
        [long_rejection, short_rejection, long_acceptance, short_acceptance],
        ["LONG", "SHORT", "LONG", "SHORT"],
        default="",
    )
    work["lane"] = np.select(
        [long_rejection, short_rejection, long_acceptance, short_acceptance],
        ["BALANCE_REJECTION", "BALANCE_REJECTION", "VALUE_ACCEPTANCE", "VALUE_ACCEPTANCE"],
        default="",
    )
    return work.loc[work.direction.ne("") & work.entry_open.notna()].groupby("day", sort=True).head(1)


def structural_levels(row: Any, entry_price: float) -> tuple[float, float] | None:
    if row.lane == "BALANCE_REJECTION":
        if row.direction == "LONG":
            return entry_price * 0.99, float(row.prev_poc)
        return entry_price * 1.01, float(row.prev_poc)

    if row.direction == "LONG":
        structural_risk = entry_price - float(row.prev_vah) * 0.999
        risk = max(entry_price * 0.005, structural_risk)
        if risk > entry_price * 0.012:
            return None
        return entry_price - risk, entry_price + 2 * risk
    structural_risk = float(row.prev_val) * 1.001 - entry_price
    risk = max(entry_price * 0.005, structural_risk)
    if risk > entry_price * 0.012:
        return None
    return entry_price + risk, entry_price - 2 * risk


def exit_path(
    path: pd.DataFrame,
    direction: str,
    stop: float,
    target: float,
) -> tuple[float, pd.Timestamp, str]:
    for row in path.head(MAX_HOLD_BARS).itertuples(index=False):
        if direction == "LONG":
            if row.open <= stop:
                return float(row.open), row.time, "STOP_GAP"
            if row.open >= target:
                return float(row.open), row.time, "TARGET_GAP"
            if row.low <= stop:
                return stop, row.time, "STOP"
            if row.high >= target:
                return target, row.time, "TARGET"
        else:
            if row.open >= stop:
                return float(row.open), row.time, "STOP_GAP"
            if row.open <= target:
                return float(row.open), row.time, "TARGET_GAP"
            if row.high >= stop:
                return stop, row.time, "STOP"
            if row.low <= target:
                return target, row.time, "TARGET"
    final_path = path.head(MAX_HOLD_BARS)
    last = final_path.iloc[-1]
    return float(last.close), last.time, "TIME_EXIT" if len(path) > MAX_HOLD_BARS else "SESSION_END"


def funding_return(
    funding: pd.DataFrame,
    entry_time: pd.Timestamp,
    exit_time: pd.Timestamp,
    direction: str,
) -> float:
    if funding.empty:
        return 0.0
    paid = float(funding.loc[funding.time.gt(entry_time) & funding.time.le(exit_time), "funding_rate"].sum())
    return paid if direction == "LONG" else -paid


def simulate(frame: pd.DataFrame, funding: pd.DataFrame) -> pd.DataFrame:
    candidates = identify_candidates(frame)
    sessions = {day: group.reset_index(drop=True) for day, group in frame.groupby("day", sort=False)}
    trades: list[dict[str, Any]] = []
    for row in candidates.itertuples(index=False):
        session = sessions[row.day]
        entry_index = int(row.bar_index) + 1
        entry_row = session.iloc[entry_index]
        entry_price = float(entry_row.open)
        levels = structural_levels(row, entry_price)
        if levels is None:
            continue
        stop, target = levels
        path = session.iloc[entry_index:]
        exit_price, exit_time, reason = exit_path(path, row.direction, stop, target)
        sign = 1.0 if row.direction == "LONG" else -1.0
        trades.append(
            {
                "day": row.day,
                "signal_time": row.time,
                "entry_time": entry_row.time,
                "exit_time": exit_time,
                "direction": row.direction,
                "lane": row.lane,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "stop": stop,
                "target": target,
                "rvol": float(row.rvol),
                "rvol3": float(row.rvol3) if pd.notna(row.rvol3) else None,
                "value_area_overlap": float(row.value_area_overlap),
                "prior_efficiency": float(row.prior_efficiency),
                "gross_return": sign * (exit_price / entry_price - 1.0),
                "funding_return": funding_return(funding, entry_row.time, exit_time, row.direction),
                "exit_reason": reason,
            }
        )
    return pd.DataFrame(trades)


def qualifies(report: dict[str, Any]) -> bool:
    minimum = {
        "development_2021_2023": 30,
        "validation_2024": 10,
        "test_2025": 10,
        "blind_2026_h1": 5,
    }
    windows = report["stress_windows"]
    return all(
        windows[name]["trades"] >= count
        and windows[name]["net_return"] > 0
        and windows[name]["profit_factor"] >= 1.10
        for name, count in minimum.items()
    ) and report["stress_without_top_3"]["net_return"] > 0


def run(data: Path, output: Path) -> dict[str, Any]:
    raw = load_klines(data)
    funding = load_funding(data)
    frame = prepare_dual_state(raw)
    raw_trades = simulate(frame, funding)
    base = apply_cost(raw_trades, BASE_ROUND_TRIP)
    stress = apply_cost(raw_trades, STRESS_ROUND_TRIP)
    report: dict[str, Any] = {
        "source": "Preregistered dual-state auction hypothesis",
        "data": {
            "symbol": "SOLUSDT",
            "bars": int(len(raw)),
            "first_bar": raw.time.min().isoformat(),
            "last_bar": raw.time.max().isoformat(),
        },
        "assumptions": {
            "bar_interval": "5m",
            "max_trades_per_day": 1,
            "max_hold_bars": MAX_HOLD_BARS,
            "base_round_trip_cost": BASE_ROUND_TRIP,
            "stress_round_trip_cost": STRESS_ROUND_TRIP,
            "funding": "actual Binance settlement rates while position is open",
        },
        "base_windows": window_metrics(base),
        "stress_windows": window_metrics(stress),
        "base_overall": metrics(base),
        "stress_overall": metrics(stress),
        "stress_without_top_3": remove_top_winners(stress),
        "stress_by_lane": {
            lane: metrics(group) for lane, group in stress.groupby("lane", sort=True)
        } if not stress.empty else {},
    }
    report["qualified"] = qualifies(report)
    report["decision"] = "eligible_for_shadow_review" if report["qualified"] else "reject_before_shadow_or_live"
    output.mkdir(parents=True, exist_ok=True)
    pd.concat(
        [base.assign(cost_case="base"), stress.assign(cost_case="stress")],
        ignore_index=True,
    ).to_parquet(output / "trades.parquet", index=False)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    args = parse_args()
    print(json.dumps(run(args.data, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
