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
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_30d_momentum_adaptive_v2" / "reentry_audit.json"
BASE_COST_PCT = 0.36
STRESS_COST_PCT = 0.60


def profit_factor(values: Iterable[float]) -> float:
    rows = [float(value) for value in values]
    gains = sum(value for value in rows if value > 0)
    losses = -sum(value for value in rows if value < 0)
    return gains / losses if losses > 0 else (999.0 if gains > 0 else 0.0)


def simulate_path(
    scoped: pd.DataFrame,
    start_available_ms: int,
    *,
    direction: str,
    entry_atr: float,
    stop_atr: float,
    reward_r: float,
    max_stop_pct: float,
    hold_hours: int,
) -> dict[str, Any] | None:
    start_position = int(scoped.index.searchsorted(start_available_ms))
    path = scoped.iloc[start_position : start_position + hold_hours]
    if path.empty:
        return None
    entry = float(path.iloc[0].open)
    if not np.isfinite(entry) or entry <= 0 or not np.isfinite(entry_atr) or entry_atr <= 0:
        return None
    sign = 1.0 if direction == "LONG" else -1.0
    stop_distance = min(stop_atr * entry_atr, entry * max_stop_pct / 100.0)
    stop = entry - sign * stop_distance
    take = entry + sign * stop_distance * reward_r
    exit_price = float(path.iloc[-1].close)
    outcome = "TIME"
    exit_ms = int(path.index[-1]) + 3_600_000
    for timestamp, bar in path.iterrows():
        if sign > 0:
            if bar.open <= stop:
                exit_price = float(bar.open); outcome = "STOP"; exit_ms = int(timestamp) + 3_600_000; break
            if bar.open >= take:
                exit_price = take; outcome = "TAKE"; exit_ms = int(timestamp) + 3_600_000; break
            if bar.low <= stop:
                exit_price = stop; outcome = "STOP"; exit_ms = int(timestamp) + 3_600_000; break
            if bar.high >= take:
                exit_price = take; outcome = "TAKE"; exit_ms = int(timestamp) + 3_600_000; break
        else:
            if bar.open >= stop:
                exit_price = float(bar.open); outcome = "STOP"; exit_ms = int(timestamp) + 3_600_000; break
            if bar.open <= take:
                exit_price = take; outcome = "TAKE"; exit_ms = int(timestamp) + 3_600_000; break
            if bar.high >= stop:
                exit_price = stop; outcome = "STOP"; exit_ms = int(timestamp) + 3_600_000; break
            if bar.low <= take:
                exit_price = take; outcome = "TAKE"; exit_ms = int(timestamp) + 3_600_000; break
    gross_pct = sign * (exit_price / entry - 1.0) * 100.0
    return {
        "entry_ms": start_available_ms,
        "exit_ms": exit_ms,
        "entry": entry,
        "exit_price": exit_price,
        "outcome": outcome,
        "gross_pct": gross_pct,
        "stop": stop,
        "take": take,
    }


def simulate_with_reentry(
    signals: pd.DataFrame,
    bars: dict[str, pd.DataFrame],
    profile: Any,
    *,
    reward_r: float = 3.5,
    stop_atr: float = 2.5,
    reentry_max_hours: int = 24,
    cost_pct: float = BASE_COST_PCT,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for signal in signals.itertuples(index=False):
        scoped = bars.get(signal.symbol)
        entry_available_ms = int(signal.available_ms) + 3_600_000
        if scoped is None or entry_available_ms not in scoped.index:
            continue
        first = simulate_path(
            scoped,
            entry_available_ms,
            direction=signal.direction,
            entry_atr=float(signal.atr_24h),
            stop_atr=stop_atr,
            reward_r=reward_r,
            max_stop_pct=profile.max_stop_pct,
            hold_hours=profile.hold_hours,
        )
        if first is None:
            continue
        rows.append(
            {
                "signal_ms": int(signal.available_ms),
                "entry_ms": first["entry_ms"],
                "exit_ms": first["exit_ms"],
                "symbol": signal.symbol,
                "direction": signal.direction,
                "strength": float(signal.strength),
                "outcome": first["outcome"],
                "gross_pct": first["gross_pct"],
                "cost_pct": cost_pct,
                "net_pct": first["gross_pct"] - cost_pct,
                "reentry": 0,
            }
        )
        if first["outcome"] == "STOP" and (
            first["exit_ms"] - first["entry_ms"] <= reentry_max_hours * 3_600_000
        ):
            second = simulate_path(
                scoped,
                first["exit_ms"],
                direction=signal.direction,
                entry_atr=float(signal.atr_24h),
                stop_atr=stop_atr,
                reward_r=reward_r,
                max_stop_pct=profile.max_stop_pct,
                hold_hours=profile.hold_hours,
            )
            if second is not None:
                rows.append(
                    {
                        "signal_ms": int(signal.available_ms),
                        "entry_ms": second["entry_ms"],
                        "exit_ms": second["exit_ms"],
                        "symbol": signal.symbol,
                        "direction": signal.direction,
                        "strength": float(signal.strength) - 0.05,
                        "outcome": second["outcome"],
                        "gross_pct": second["gross_pct"],
                        "cost_pct": cost_pct,
                        "net_pct": second["gross_pct"] - cost_pct,
                        "reentry": 1,
                    }
                )
    return pd.DataFrame(rows)


def gate_single_position(
    trades: pd.DataFrame,
    *,
    min_history: int = 3,
    history_size: int = 8,
    min_pf: float = 1.25,
) -> pd.DataFrame:
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit same-day re-entry after an early stop for 30-day alt momentum."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--reentry-max-hours", type=int, default=24)
    args = parser.parse_args()
    candidate = next(item for item in CANDIDATES if item.name == "momentum_30d_hold_7d")
    profile = adaptive_profile()
    raw_parts: list[pd.DataFrame] = []
    for index, data_dir in enumerate(DEFAULT_DATA, start=1):
        starts, _ = load_manifest(data_dir)
        panel = add_slow_returns(build_panel(data_dir, starts))
        signals = slow_momentum_signals(panel, candidate, minimum_age_days=45)
        signals = signals.loc[
            signals.market_breadth.abs().between(BREADTH_LOW, BREADTH_HIGH)
        ]
        bars = {
            symbol: scoped.sort_values("available_ms").set_index("available_ms")
            for symbol, scoped in panel.groupby("symbol", sort=False)
        }
        raw_parts.append(
            simulate_with_reentry(
                signals,
                bars,
                profile,
                reentry_max_hours=args.reentry_max_hours,
            )
        )
        del bars, panel, signals
        gc.collect()
    raw = pd.concat(raw_parts, ignore_index=True)
    base = raw.loc[raw.reentry.eq(0)].copy()
    with_reentry = gate_single_position(raw)
    base_gated = gate_single_position(base)
    reports: dict[str, Any] = {}
    for name, frame in (("baseline", base_gated), ("with_reentry", with_reentry)):
        stress = frame.assign(net_pct=frame.gross_pct - STRESS_COST_PCT)
        stress["year"] = pd.to_datetime(stress.entry_ms, unit="ms", utc=True).dt.year
        report = {
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
        reports[name] = report
        print(f"{name}: trades={report['trades']} PF={report['profit_factor']} "
              f"final={report['equity']['final_equity']}U", flush=True)
    reentry_rows = raw.loc[raw.reentry.eq(1)]
    print(f"reentry_candidates={len(reentry_rows)} "
          f"reentry_PF={profit_factor(reentry_rows.gross_pct - STRESS_COST_PCT):.3f}", flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "experiment": "s0_altcoin_30d_same_day_reentry",
                "reentry_max_hours": args.reentry_max_hours,
                "reports": reports,
                "reentry_candidates": int(len(reentry_rows)),
                "reentry_pf": round(profit_factor(reentry_rows.gross_pct - STRESS_COST_PCT), 6),
                "warning": "Historical qualification is not live-trading approval.",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
