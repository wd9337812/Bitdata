from __future__ import annotations

import argparse
import gc
import heapq
import json
import sys
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_s0_point_in_time_slow_momentum import (  # noqa: E402
    CANDIDATES,
    add_slow_returns,
    slow_momentum_signals,
)
from scripts.benchmark_s0_xmom_point_in_time import (  # noqa: E402
    build_panel,
    load_manifest,
)
from scripts.benchmark_s0_adaptive_30d_momentum import (  # noqa: E402
    BREADTH_HIGH,
    BREADTH_LOW,
    adaptive_profile,
)

DEFAULT_DATA = (
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2020_2023",
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2024_2025",
    ROOT / "data" / "research" / "binance_um_point_in_time_1h",
)
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_30d_momentum_adaptive_v2" / "exit_profiles_audit.json"
BASE_COST_PCT = 0.36
STRESS_COST_PCT = 0.60


def profit_factor(values: Iterable[float]) -> float:
    rows = [float(value) for value in values]
    gains = sum(value for value in rows if value > 0)
    losses = -sum(value for value in rows if value < 0)
    return gains / losses if losses > 0 else (999.0 if gains > 0 else 0.0)


def gate_single_position(
    trades: pd.DataFrame,
    *,
    min_history: int = 3,
    history_size: int = 8,
    min_pf: float = 1.25,
) -> pd.DataFrame:
    """Direction-gated single-slot allocation that can skip weaker overlapping bets."""
    ordered = trades.sort_values(
        ["entry_ms", "strength"], ascending=[True, False]
    ).reset_index(drop=True)
    history: dict[str, list[float]] = {direction: [] for direction in ("LONG", "SHORT")}
    pending: list[tuple[int, int, str, float]] = []
    active_exit = -1
    selected: list[int] = []
    for sequence, row in enumerate(ordered.itertuples()):
        entry_ms = int(row.entry_ms)
        while pending and pending[0][0] <= entry_ms:
            _, _, direction, net_pct = heapq.heappop(pending)
            history[direction].append(net_pct)
        direction = str(row.direction)
        recent = history[direction][-history_size:]
        allowed = len(recent) < min_history or profit_factor(recent) >= min_pf
        if allowed and entry_ms >= active_exit:
            selected.append(int(row.Index))
            active_exit = int(row.exit_ms)
        heapq.heappush(
            pending,
            (int(row.exit_ms), sequence, direction, float(row.net_pct)),
        )
    return ordered.loc[selected].reset_index(drop=True)


def simulate_exit(
    signals: pd.DataFrame,
    bars: dict[str, pd.DataFrame],
    profile: Any,
    *,
    first_r: float,
    first_fraction: float,
    second_r: float | None = None,
    trail_r: float | None = None,
    breakeven_after_first: bool = False,
    stop_atr: float | None = None,
    hold_hours: int | None = None,
    cost_pct: float = BASE_COST_PCT,
) -> pd.DataFrame:
    trades: list[dict[str, Any]] = []
    effective_stop_atr = float(stop_atr if stop_atr is not None else profile.stop_atr)
    effective_hold = int(hold_hours if hold_hours is not None else profile.hold_hours)
    for signal in signals.itertuples(index=False):
        scoped = bars.get(signal.symbol)
        entry_bar_available_ms = int(signal.available_ms) + 3_600_000
        if scoped is None or entry_bar_available_ms not in scoped.index:
            continue
        start_position = int(scoped.index.searchsorted(entry_bar_available_ms))
        path = scoped.iloc[start_position : start_position + effective_hold]
        if path.empty:
            continue
        entry = float(path.iloc[0].open)
        atr = float(signal.atr_24h)
        if not np.isfinite(entry) or not np.isfinite(atr) or entry <= 0 or atr <= 0:
            continue
        sign = 1.0 if signal.direction == "LONG" else -1.0
        stop_distance = min(
            effective_stop_atr * atr,
            entry * profile.max_stop_pct / 100.0,
        )
        stop = entry - sign * stop_distance
        first_target = entry + sign * stop_distance * first_r
        second_target = (
            entry + sign * stop_distance * second_r if second_r is not None else None
        )
        trail_distance = stop_distance * trail_r if trail_r is not None else None
        remaining = 1.0
        realized = 0.0
        exit_ms = int(path.index[-1]) + 3_600_000
        trail_stop = stop
        for timestamp, bar in path.iterrows():
            high = float(bar.high)
            low = float(bar.low)
            if sign > 0:
                if trail_distance is not None:
                    trail_stop = max(trail_stop, high - trail_distance)
                if low <= trail_stop and remaining < 1.0:
                    realized += remaining * sign * (trail_stop / entry - 1.0) * 100.0
                    remaining = 0.0
                    exit_ms = int(timestamp) + 3_600_000
                    break
                if bar.open <= stop:
                    realized += remaining * sign * (stop / entry - 1.0) * 100.0
                    remaining = 0.0
                    exit_ms = int(timestamp) + 3_600_000
                    break
                if remaining > 0 and first_fraction > 0 and bar.open >= first_target:
                    realized += first_fraction * sign * (first_target / entry - 1.0) * 100.0
                    remaining -= first_fraction
                    if breakeven_after_first:
                        stop = entry
                    if remaining <= 0:
                        exit_ms = int(timestamp) + 3_600_000
                        break
                if (
                    remaining > 0
                    and second_target is not None
                    and bar.open >= second_target
                ):
                    realized += remaining * sign * (second_target / entry - 1.0) * 100.0
                    remaining = 0.0
                    exit_ms = int(timestamp) + 3_600_000
                    break
                stop_hit = low <= stop
                take1_hit = high >= first_target
                take2_hit = second_target is not None and high >= second_target
            else:
                if trail_distance is not None:
                    trail_stop = min(trail_stop, low + trail_distance)
                if high >= trail_stop and remaining < 1.0:
                    realized += remaining * sign * (trail_stop / entry - 1.0) * 100.0
                    remaining = 0.0
                    exit_ms = int(timestamp) + 3_600_000
                    break
                if bar.open >= stop:
                    realized += remaining * sign * (stop / entry - 1.0) * 100.0
                    remaining = 0.0
                    exit_ms = int(timestamp) + 3_600_000
                    break
                if remaining > 0 and first_fraction > 0 and bar.open <= first_target:
                    realized += first_fraction * sign * (first_target / entry - 1.0) * 100.0
                    remaining -= first_fraction
                    if breakeven_after_first:
                        stop = entry
                    if remaining <= 0:
                        exit_ms = int(timestamp) + 3_600_000
                        break
                if (
                    remaining > 0
                    and second_target is not None
                    and bar.open <= second_target
                ):
                    realized += remaining * sign * (second_target / entry - 1.0) * 100.0
                    remaining = 0.0
                    exit_ms = int(timestamp) + 3_600_000
                    break
                stop_hit = high >= stop
                take1_hit = low <= first_target
                take2_hit = second_target is not None and low <= second_target
            if remaining <= 0:
                break
            if stop_hit:
                realized += remaining * sign * (stop / entry - 1.0) * 100.0
                remaining = 0.0
                exit_ms = int(timestamp) + 3_600_000
                break
            if take1_hit and first_fraction > 0:
                realized += first_fraction * sign * (first_target / entry - 1.0) * 100.0
                remaining -= first_fraction
                if breakeven_after_first:
                    stop = entry
                if remaining <= 0:
                    exit_ms = int(timestamp) + 3_600_000
                    break
            if take2_hit and second_target is not None:
                realized += remaining * sign * (second_target / entry - 1.0) * 100.0
                remaining = 0.0
                exit_ms = int(timestamp) + 3_600_000
                break
        if remaining > 0:
            realized += remaining * sign * (float(path.iloc[-1].close) / entry - 1.0) * 100.0
        gross_pct = realized
        trades.append(
            {
                "signal_ms": int(signal.available_ms),
                "entry_ms": int(signal.available_ms),
                "exit_ms": exit_ms,
                "symbol": signal.symbol,
                "direction": signal.direction,
                "strength": float(signal.strength),
                "gross_pct": gross_pct,
                "cost_pct": cost_pct,
                "net_pct": gross_pct - cost_pct,
            }
        )
    return pd.DataFrame(trades)


def simulate_equity(
    net_pct_points: Iterable[float],
    *,
    start_equity: float = 15.0,
    hard_stop: float = 5.0,
    risk_pct: float = 20.0,
    reference_stop_pct: float = 15.0,
) -> dict[str, Any]:
    equity = float(start_equity)
    peak = equity
    max_drawdown = 0.0
    stopped = False
    for value in net_pct_points:
        equity *= 1 + float(value) / 100 * risk_pct / reference_stop_pct
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak * 100 if peak > 0 else 0)
        if equity <= hard_stop:
            stopped = True
            break
    return {
        "final_equity": round(equity, 8),
        "max_drawdown_pct": round(max_drawdown, 6),
        "hard_stop_hit": stopped,
    }


EXIT_PROFILES = {
    "full_2R": {"first_r": 2.0, "first_fraction": 1.0},
    "full_2_5R": {"first_r": 2.5, "first_fraction": 1.0},
    "full_3R": {"first_r": 3.0, "first_fraction": 1.0},
    "full_3_5R": {"first_r": 3.5, "first_fraction": 1.0},
    "full_4R": {"first_r": 4.0, "first_fraction": 1.0},
    "split_1_5_3": {"first_r": 1.5, "first_fraction": 0.5, "second_r": 3.0},
    "split_1_25_2_5": {"first_r": 1.25, "first_fraction": 0.5, "second_r": 2.5},
    "breakeven_1_5_then_3": {
        "first_r": 1.5,
        "first_fraction": 0.5,
        "second_r": 3.0,
        "breakeven_after_first": True,
    },
    "trail_1_5_then_1R": {"first_r": 1.5, "first_fraction": 0.5, "trail_r": 1.0},
    "full_3_5R_stop2_hold120": {"first_r": 3.5, "first_fraction": 1.0, "stop_atr": 2.0},
    "full_3_5R_stop3_hold120": {"first_r": 3.5, "first_fraction": 1.0, "stop_atr": 3.0},
    "full_3_5R_stop2_hold168": {
        "first_r": 3.5,
        "first_fraction": 1.0,
        "stop_atr": 2.0,
        "hold_hours": 168,
    },
    "full_3_5R_stop2_5_hold168": {
        "first_r": 3.5,
        "first_fraction": 1.0,
        "hold_hours": 168,
    },
    "full_3_5R_stop3_hold168": {
        "first_r": 3.5,
        "first_fraction": 1.0,
        "stop_atr": 3.0,
        "hold_hours": 168,
    },
    "full_3R_stop2_5_hold168": {
        "first_r": 3.0,
        "first_fraction": 1.0,
        "hold_hours": 168,
    },
    "full_4R_stop2_5_hold168": {
        "first_r": 4.0,
        "first_fraction": 1.0,
        "hold_hours": 168,
    },
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare exit profiles on the frozen 30-day alt momentum selector."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    candidate = next(item for item in CANDIDATES if item.name == "momentum_30d_hold_7d")
    profile = adaptive_profile()
    raw_parts: dict[str, list[pd.DataFrame]] = {
        name: [] for name in EXIT_PROFILES
    }
    for index, data_dir in enumerate(DEFAULT_DATA, start=1):
        starts, _ = load_manifest(data_dir)
        panel = add_slow_returns(build_panel(data_dir, starts))
        signals = slow_momentum_signals(panel, candidate, minimum_age_days=45)
        signals = signals.loc[
            signals.market_breadth.abs().between(BREADTH_LOW, BREADTH_HIGH)
        ]
        print(f"dir{index}: panel={len(panel)} signals={len(signals)}", flush=True)
        bars = {
            symbol: scoped.sort_values("available_ms").set_index("available_ms")
            for symbol, scoped in panel.groupby("symbol", sort=False)
        }
        for name, cfg in EXIT_PROFILES.items():
            raw_parts[name].append(
                simulate_exit(
                    signals,
                    bars,
                    profile,
                    cost_pct=BASE_COST_PCT,
                    **cfg,
                )
            )
        del bars, panel, signals
        gc.collect()

    reports: dict[str, Any] = {}
    for name in EXIT_PROFILES:
        raw = pd.concat(raw_parts[name], ignore_index=True)
        funded = gate_single_position(raw)
        print(f"{name}: raw={len(raw)} gated={len(funded)}", flush=True)
        stress = funded.assign(net_pct=funded.gross_pct - STRESS_COST_PCT)
        stress["year"] = pd.to_datetime(stress.entry_ms, unit="ms", utc=True).dt.year
        reports[name] = {
            "trades": int(len(stress)),
            "symbols": int(stress["symbol"].nunique()),
            "profit_factor": round(profit_factor(stress["net_pct"]), 6),
            "net_pct_points": round(float(stress["net_pct"].sum()), 6),
            "equity": simulate_equity(stress["net_pct"]),
            "by_year": {
                str(int(year)): simulate_equity(rows["net_pct"])
                for year, rows in stress.groupby("year")
            },
        }
        print(f"{name}: trades={reports[name]['trades']} "
              f"PF={reports[name]['profit_factor']} "
              f"final={reports[name]['equity']['final_equity']}U", flush=True)
    del raw_parts
    gc.collect()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "experiment": "s0_altcoin_30d_exit_profiles",
                "exit_profiles": EXIT_PROFILES,
                "reports": reports,
                "warning": "Historical qualification is not live-trading approval.",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
