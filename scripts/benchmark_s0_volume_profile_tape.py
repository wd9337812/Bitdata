from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data" / "research" / "binance_um_prefunding_5m"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_volume_profile_tape"
BASE_ROUND_TRIP = 0.0014
STRESS_ROUND_TRIP = 0.0024
BIN_WIDTH = 0.001
VALUE_AREA_SHARE = 0.70


@dataclass(frozen=True)
class Parameters:
    tape_rvol: float
    stop_pct: float

    @property
    def name(self) -> str:
        return f"rvol{self.tape_rvol:.1f}_sl{self.stop_pct:.3f}"


# Frozen before inspecting candidate returns. The paper discloses 1%-2% stops
# but not its gated Tape Speed formula. Public implementations define speed as
# directional relative volume, so this small grid tests that explicit proxy.
PARAMETER_GRID = tuple(
    Parameters(tape_rvol, stop_pct)
    for tape_rvol in (1.0, 1.5, 2.0)
    for stop_pct in (0.010, 0.015, 0.020)
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit a prior-day volume-profile mean-reversion proxy on SOLUSDT."
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def _price_bin(price: pd.Series | np.ndarray, width: float = BIN_WIDTH) -> np.ndarray:
    values = np.asarray(price, dtype=float)
    return np.rint(np.log(values) / width).astype(np.int64)


def profile_levels(
    frame: pd.DataFrame,
    value_area_share: float = VALUE_AREA_SHARE,
    bin_width: float = BIN_WIDTH,
) -> dict[str, float]:
    if frame.empty:
        raise ValueError("Cannot build a profile from an empty frame")
    typical = (frame.high.astype(float) + frame.low.astype(float) + frame.close.astype(float)) / 3
    bins = _price_bin(typical, bin_width)
    weights = pd.Series(frame.quote_volume.astype(float).to_numpy(), index=bins).groupby(level=0).sum()
    weights = weights.sort_index()
    poc_pos = int(np.argmax(weights.to_numpy()))
    selected = {poc_pos}
    total = float(weights.sum())
    covered = float(weights.iloc[poc_pos])
    left = poc_pos - 1
    right = poc_pos + 1
    while covered < total * value_area_share and (left >= 0 or right < len(weights)):
        left_weight = float(weights.iloc[left]) if left >= 0 else -1.0
        right_weight = float(weights.iloc[right]) if right < len(weights) else -1.0
        if right_weight > left_weight:
            selected.add(right)
            covered += right_weight
            right += 1
        else:
            selected.add(left)
            covered += left_weight
            left -= 1
    selected_bins = weights.index[list(selected)].to_numpy(dtype=float)
    poc_bin = float(weights.index[poc_pos])
    return {
        "poc": float(np.exp(poc_bin * bin_width)),
        "val": float(np.exp((selected_bins.min() - 0.5) * bin_width)),
        "vah": float(np.exp((selected_bins.max() + 0.5) * bin_width)),
        "coverage": covered / total if total > 0 else 0.0,
    }


def load_klines(root: Path) -> pd.DataFrame:
    paths = sorted((root / "klines" / "SOLUSDT").glob("*.parquet"))
    if not paths:
        raise FileNotFoundError(f"No SOLUSDT 5m klines below {root}")
    columns = ["open_time", "open", "high", "low", "close", "close_time", "quote_volume"]
    frame = pd.concat([pd.read_parquet(path, columns=columns) for path in paths], ignore_index=True)
    frame = frame.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)
    for column in columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna().reset_index(drop=True)
    frame["time"] = pd.to_datetime(frame.open_time, unit="ms", utc=True)
    frame["day"] = frame.time.dt.floor("D")
    frame["rvol"] = frame.quote_volume / frame.quote_volume.rolling(30, min_periods=30).mean().shift(1)
    frame["pulse"] = np.sign(frame.close.diff()) * frame.rvol
    return frame


def load_funding(root: Path) -> pd.DataFrame:
    paths = sorted((root / "fundingRate" / "SOLUSDT").glob("*.parquet"))
    if not paths:
        return pd.DataFrame(columns=["time", "funding_rate"])
    parts = [pd.read_parquet(path) for path in paths]
    frame = pd.concat(parts, ignore_index=True).drop_duplicates("calc_time").sort_values("calc_time")
    frame["time"] = pd.to_datetime(pd.to_numeric(frame.calc_time), unit="ms", utc=True)
    frame["funding_rate"] = pd.to_numeric(frame.last_funding_rate, errors="coerce")
    return frame.loc[:, ["time", "funding_rate"]].dropna().reset_index(drop=True)


def prepare_sessions(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict[pd.Timestamp, dict[str, float]]]:
    counts = frame.groupby("day").size()
    complete_days = counts.loc[counts.eq(288)].index
    complete = frame.loc[frame.day.isin(complete_days)].copy()
    profiles = {
        day: profile_levels(group)
        for day, group in complete.groupby("day", sort=True)
    }
    days = sorted(profiles)
    previous = {days[i]: profiles[days[i - 1]] for i in range(1, len(days)) if days[i] - days[i - 1] == pd.Timedelta(days=1)}
    complete = complete.loc[complete.day.isin(previous)].copy().reset_index(drop=True)

    current_poc = np.empty(len(complete), dtype=float)
    for _, group in complete.groupby("day", sort=True):
        cumulative: dict[int, float] = {}
        best_bin = 0
        best_weight = -1.0
        typical = (group.high.to_numpy(float) + group.low.to_numpy(float) + group.close.to_numpy(float)) / 3
        bins = _price_bin(typical)
        for index, price_bin, volume in zip(group.index, bins, group.quote_volume.to_numpy(float)):
            cumulative[int(price_bin)] = cumulative.get(int(price_bin), 0.0) + float(volume)
            if cumulative[int(price_bin)] > best_weight:
                best_bin = int(price_bin)
                best_weight = cumulative[int(price_bin)]
            current_poc[int(index)] = float(np.exp(best_bin * BIN_WIDTH))
    complete["current_poc"] = current_poc
    complete["prev_val"] = complete.day.map(lambda day: previous[day]["val"])
    complete["prev_vah"] = complete.day.map(lambda day: previous[day]["vah"])
    complete["prev_poc"] = complete.day.map(lambda day: previous[day]["poc"])
    return complete, previous


def _funding_return(
    funding: pd.DataFrame,
    entry_time: pd.Timestamp,
    exit_time: pd.Timestamp,
    direction: str,
) -> float:
    if funding.empty:
        return 0.0
    paid = float(funding.loc[funding.time.gt(entry_time) & funding.time.le(exit_time), "funding_rate"].sum())
    return paid if direction == "LONG" else -paid


def _exit_trade(
    path: pd.DataFrame,
    direction: str,
    entry_price: float,
    target: float,
    stop_pct: float,
) -> tuple[float, pd.Timestamp, str]:
    stop = entry_price * (1 - stop_pct) if direction == "LONG" else entry_price * (1 + stop_pct)
    for row in path.itertuples(index=False):
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
    last = path.iloc[-1]
    return float(last.close), last.time, "SESSION_END"


def simulate(
    frame: pd.DataFrame,
    funding: pd.DataFrame,
    params: Parameters,
) -> pd.DataFrame:
    trades: list[dict[str, Any]] = []
    work = frame.copy()
    work["bar_index"] = work.groupby("day").cumcount()
    work["entry_open"] = work.groupby("day").open.shift(-1)
    long_signal = (
        work.low.le(work.prev_val)
        & work.close.gt(work.prev_val)
        & work.pulse.gt(0)
        & work.rvol.ge(params.tape_rvol)
        & work.current_poc.gt(work.entry_open)
    )
    short_signal = (
        work.high.ge(work.prev_vah)
        & work.close.lt(work.prev_vah)
        & work.pulse.lt(0)
        & work.rvol.ge(params.tape_rvol)
        & work.current_poc.lt(work.entry_open)
    )
    work["direction"] = np.where(long_signal, "LONG", np.where(short_signal, "SHORT", ""))
    candidates = work.loc[work.direction.ne("") & work.entry_open.notna()].groupby("day", sort=True).head(1)
    sessions = {day: group.reset_index(drop=True) for day, group in work.groupby("day", sort=False)}
    for row in candidates.itertuples(index=False):
        session = sessions[row.day]
        entry_index = int(row.bar_index) + 1
        entry_row = session.iloc[entry_index]
        entry_price = float(entry_row.open)
        target = float(row.current_poc)
        path = session.iloc[entry_index:]
        exit_price, exit_time, reason = _exit_trade(
            path, row.direction, entry_price, target, params.stop_pct
        )
        sign = 1.0 if row.direction == "LONG" else -1.0
        gross = sign * (exit_price / entry_price - 1.0)
        funding_paid = _funding_return(funding, entry_row.time, exit_time, row.direction)
        trades.append(
            {
                "parameters": params.name,
                "day": row.day,
                "signal_time": row.time,
                "entry_time": entry_row.time,
                "exit_time": exit_time,
                "direction": row.direction,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "target": target,
                "rvol": float(row.rvol),
                "gross_return": gross,
                "funding_return": funding_paid,
                "exit_reason": reason,
            }
        )
    return pd.DataFrame(trades)


def apply_cost(trades: pd.DataFrame, round_trip_cost: float) -> pd.DataFrame:
    result = trades.copy()
    if result.empty:
        return result
    result["cost"] = round_trip_cost
    result["net_return"] = result.gross_return - result.funding_return - round_trip_cost
    return result


def metrics(trades: pd.DataFrame) -> dict[str, float | int]:
    if trades.empty:
        return {
            "trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "net_return": 0.0,
            "average_net_return": 0.0,
            "max_drawdown": 0.0,
        }
    returns = trades.net_return.astype(float)
    gains = float(returns.loc[returns.gt(0)].sum())
    losses = float(-returns.loc[returns.lt(0)].sum())
    curve = (1 + returns).cumprod()
    drawdown = curve / curve.cummax() - 1
    return {
        "trades": int(len(trades)),
        "win_rate": float(returns.gt(0).mean()),
        "profit_factor": gains / losses if losses > 0 else 999.0,
        "net_return": float(returns.sum()),
        "average_net_return": float(returns.mean()),
        "max_drawdown": float(drawdown.min()),
    }


WINDOWS = {
    "development_2021_2023": ("2021-01-01", "2024-01-01"),
    "validation_2024": ("2024-01-01", "2025-01-01"),
    "test_2025": ("2025-01-01", "2026-01-01"),
    "blind_2026_h1": ("2026-01-01", "2026-07-01"),
}


def window_metrics(trades: pd.DataFrame) -> dict[str, dict[str, float | int]]:
    return {
        name: metrics(
            trades.loc[
                trades.entry_time.ge(pd.Timestamp(start, tz="UTC"))
                & trades.entry_time.lt(pd.Timestamp(end, tz="UTC"))
            ]
        )
        for name, (start, end) in WINDOWS.items()
    }


def remove_top_winners(trades: pd.DataFrame, count: int = 3) -> dict[str, float | int]:
    if trades.empty:
        return metrics(trades)
    return metrics(trades.drop(trades.nlargest(min(count, len(trades)), "net_return").index))


def qualifies(report: dict[str, Any]) -> bool:
    minimum = {
        "development_2021_2023": 30,
        "validation_2024": 10,
        "test_2025": 10,
        "blind_2026_h1": 5,
    }
    windows = report["stress_windows"]
    return all(
        windows[name]["trades"] >= trades
        and windows[name]["net_return"] > 0
        and windows[name]["profit_factor"] >= 1.10
        for name, trades in minimum.items()
    ) and report["stress_without_top_3"]["net_return"] > 0


def run(data: Path, output: Path) -> dict[str, Any]:
    raw = load_klines(data)
    funding = load_funding(data)
    frame, profiles = prepare_sessions(raw)
    base_parts: list[pd.DataFrame] = []
    stress_parts: list[pd.DataFrame] = []
    reports: dict[str, Any] = {}
    for params in PARAMETER_GRID:
        raw_trades = simulate(frame, funding, params)
        base = apply_cost(raw_trades, BASE_ROUND_TRIP)
        stress = apply_cost(raw_trades, STRESS_ROUND_TRIP)
        if not base.empty:
            base["cost_case"] = "base"
            base_parts.append(base)
        if not stress.empty:
            stress["cost_case"] = "stress"
            stress_parts.append(stress)
        report = {
            "parameters": asdict(params),
            "base_windows": window_metrics(base),
            "stress_windows": window_metrics(stress),
            "base_overall": metrics(base),
            "stress_overall": metrics(stress),
            "stress_without_top_3": remove_top_winners(stress),
        }
        report["qualified"] = qualifies(report)
        reports[params.name] = report
    trade_parts = base_parts + stress_parts
    all_trades = pd.concat(trade_parts, ignore_index=True) if trade_parts else pd.DataFrame()
    qualified = [name for name, report in reports.items() if report["qualified"]]
    result = {
        "source": "SSRN 6932998; proxy because full formula was unavailable",
        "data": {
            "symbol": "SOLUSDT",
            "bars": int(len(raw)),
            "first_bar": raw.time.min().isoformat(),
            "last_bar": raw.time.max().isoformat(),
            "complete_sessions": int(len(profiles)),
        },
        "assumptions": {
            "bar_interval": "5m",
            "profile_bin_width_log": BIN_WIDTH,
            "value_area_share": VALUE_AREA_SHARE,
            "entry": "next 5m open after VAH/VAL sweep-and-reentry with directional RVOL",
            "target": "current-session point of control frozen at signal close",
            "max_trades_per_day": 1,
            "base_round_trip_cost": BASE_ROUND_TRIP,
            "stress_round_trip_cost": STRESS_ROUND_TRIP,
            "funding": "actual Binance settlement rates while position is open",
        },
        "reports": reports,
        "qualified": qualified,
        "decision": "eligible_for_shadow_review" if qualified else "reject_before_shadow_or_live",
    }
    output.mkdir(parents=True, exist_ok=True)
    all_trades.to_parquet(output / "trades.parquet", index=False)
    (output / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> None:
    args = parse_args()
    result = run(args.data, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
