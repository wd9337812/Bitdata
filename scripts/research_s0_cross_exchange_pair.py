from __future__ import annotations

import argparse
import gzip
import itertools
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.research_s0_cross_exchange_dislocation import (  # noqa: E402
    MINUTE_MS,
    build_premium,
    build_signal_matrices,
    build_signal_panel,
    select_events_fast,
)
from scripts.research_s0_intraday_xmom import (  # noqa: E402
    CandidateTrade,
    simulate_equity,
)

DEFAULT_BINANCE = ROOT / "data" / "research" / "binance_um_1m_cross_year" / "parquet"
DEFAULT_BYBIT = ROOT / "data" / "research" / "bybit_mt4_1m" / "parquet"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Two-leg cross-exchange dislocation strategy: when Binance perp price "
            "diverges from Bybit perp price, short the expensive leg and long the "
            "cheap leg; exit when the premium reverts (or widens / time)."
        )
    )
    parser.add_argument("--binance", type=Path, default=DEFAULT_BINANCE)
    parser.add_argument("--bybit", type=Path, default=DEFAULT_BYBIT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default="2026-08-06")
    parser.add_argument("--grid", action="store_true")
    parser.add_argument("--threshold-pct", type=float, default=0.20)
    parser.add_argument("--z-threshold", type=float, default=3.0)
    parser.add_argument("--decision-bar", type=int, default=5)
    parser.add_argument("--entry-delay-min", type=int, default=0)
    parser.add_argument("--min-21d-volume-usdt", type=float, default=20_000_000.0)
    parser.add_argument(
        "--min-entry-premium-pct",
        type=float,
        default=0.05,
        help="Minimum |premium| still present at entry; flipped signals are skipped.",
    )
    parser.add_argument(
        "--max-entry-premium-pct",
        type=float,
        default=999.0,
        help="Skip entries whose |premium| exceeds this cap (data-artifact guard).",
    )
    parser.add_argument(
        "--bybit-entry-delay-min",
        type=int,
        default=0,
        help="Delay Bybit leg entry by N minutes to model cross-exchange latency.",
    )
    parser.add_argument("--cost-multiplier", type=float, default=1.0)
    parser.add_argument("--revert-ratio", type=float, default=0.5)
    parser.add_argument("--stop-ratio", type=float, default=2.0)
    parser.add_argument("--max-hold-minutes", type=int, default=60)
    parser.add_argument("--cooldown-min", type=int, default=30)
    parser.add_argument("--leverage-per-leg", type=float, default=5.0)
    parser.add_argument("--binance-fee-bps", type=float, default=5.0)
    parser.add_argument("--binance-slippage-bps", type=float, default=2.0)
    parser.add_argument("--bybit-fee-bps", type=float, default=5.5)
    parser.add_argument("--bybit-slippage-bps", type=float, default=2.0)
    parser.add_argument("--initial-equity", type=float, default=14.0)
    parser.add_argument("--max-trades", type=int, default=0)
    return parser.parse_args()


def simulate_premium_reverts_vectorized(
    frame: pd.DataFrame,
    entries: np.ndarray,
    entry_premiums: np.ndarray,
    direction: int,
    revert_ratio: float,
    stop_ratio: float,
    max_hold: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Adverse-first revert/stop exit on the premium series."""
    entries = np.asarray(entries, dtype="int64")
    entry_premiums = np.asarray(entry_premiums, dtype="float64")
    count = len(entries)
    times = frame.open_time.to_numpy()
    premiums = frame.premium_pct.to_numpy()
    starts = np.searchsorted(times, entries, side="left")
    target = entry_premiums * revert_ratio
    stop = entry_premiums * stop_ratio
    exit_idx = np.full(count, -1, dtype="int64")
    outcome = np.full(count, "", dtype=object)
    hold_minutes = np.zeros(count, dtype="int64")
    active = (starts < len(times)).copy()
    for step in range(0, max_hold + 1):
        idx = starts + step
        valid = active & (idx < len(times))
        if not valid.any():
            break
        if direction == 1:
            stop_hit = premiums[idx[valid]] <= stop[valid]
            revert_hit = premiums[idx[valid]] >= target[valid]
        else:
            stop_hit = premiums[idx[valid]] >= stop[valid]
            revert_hit = premiums[idx[valid]] <= target[valid]
        stop_mask = valid.copy()
        stop_mask[valid] = stop_hit
        if stop_mask.any():
            exit_idx[stop_mask] = idx[stop_mask]
            outcome[stop_mask] = "STOP"
            hold_minutes[stop_mask] = step + 1
        revert_mask = valid.copy()
        revert_mask[valid] = revert_hit & ~stop_hit
        if revert_mask.any():
            exit_idx[revert_mask] = idx[revert_mask]
            outcome[revert_mask] = "REVERT"
            hold_minutes[revert_mask] = step + 1
        active[valid] = ~(stop_hit | revert_hit)
    remaining = active
    if remaining.any():
        end_idx = np.minimum(starts + max_hold, len(times) - 1)
        exit_idx[remaining] = end_idx[remaining]
        outcome[remaining] = "TIME"
        hold_minutes[remaining] = np.minimum(
            max_hold + 1, len(times) - starts[remaining]
        )
    return exit_idx, outcome, hold_minutes, premiums


def build_pair_trades(
    events: list[tuple[int, str, int, float]],
    premium: dict[str, pd.DataFrame],
    delay: int,
    cooldown_min: int,
    leverage_per_leg: float,
    revert_ratio: float,
    stop_ratio: float,
    max_hold: int,
    binance_fee_bps: float,
    binance_slippage_bps: float,
    bybit_fee_bps: float,
    bybit_slippage_bps: float,
    min_entry_premium_pct: float,
    max_entry_premium_pct: float,
    bybit_entry_delay_min: int,
    cost_multiplier: float,
) -> list[CandidateTrade]:
    grouped: dict[str, tuple[list[int], list[int]]] = {}
    for signal_time, symbol, direction, _premium_value in events:
        frame = premium[symbol]
        entry_floor = signal_time + delay * MINUTE_MS
        minute_times = frame.open_time.to_numpy()
        position = int(np.searchsorted(minute_times, entry_floor, side="left"))
        if position >= len(minute_times):
            continue
        entry_ms = int(minute_times[position])
        entry_premium = float(frame.premium_pct.to_numpy()[position])
        # The dislocation must still favor the signal direction at entry.
        if not np.isfinite(entry_premium):
            continue
        if direction == 1 and not (entry_premium <= -min_entry_premium_pct):
            continue
        if direction == -1 and not (entry_premium >= min_entry_premium_pct):
            continue
        if abs(entry_premium) > max_entry_premium_pct:
            continue
        bybit_position = position + bybit_entry_delay_min
        if bybit_position >= len(minute_times):
            continue
        if (
            not np.isfinite(float(frame.open.to_numpy()[position]))
            or not np.isfinite(float(frame.bybit_open.to_numpy()[bybit_position]))
            or float(frame.open.to_numpy()[position]) <= 0
            or float(frame.bybit_open.to_numpy()[bybit_position]) <= 0
        ):
            continue
        entry_times, directions = grouped.setdefault(symbol, ([], []))
        entry_times.append(entry_ms)
        directions.append(direction)
    cost_pct_equity = (
        (binance_fee_bps + binance_slippage_bps + bybit_fee_bps + bybit_slippage_bps)
        / 10000.0
        * leverage_per_leg
        / 2.0
        * 100.0
        * cost_multiplier
    )
    trades: list[CandidateTrade] = []
    for symbol, (entry_times, directions) in grouped.items():
        frame = premium[symbol]
        times_array = np.asarray(entry_times, dtype="int64")
        dirs_array = np.asarray(directions, dtype="int64")
        for direction in (1, -1):
            mask = dirs_array == direction
            if not mask.any():
                continue
            positions = np.searchsorted(
                frame.open_time.to_numpy(), times_array[mask], side="left"
            )
            entry_premiums = frame.premium_pct.to_numpy()[positions]
            exit_idx, outcome, hold, _ = simulate_premium_reverts_vectorized(
                frame,
                times_array[mask],
                entry_premiums,
                int(direction),
                revert_ratio,
                stop_ratio,
                max_hold,
            )
            binance_entries = frame.open.to_numpy()[positions]
            bybit_positions = positions + bybit_entry_delay_min
            bybit_valid = bybit_positions < len(frame)
            bybit_entries = frame.bybit_open.to_numpy()[
                np.minimum(bybit_positions, len(frame) - 1)
            ]
            for index in range(len(exit_idx)):
                if exit_idx[index] < 0:
                    continue
                if not bybit_valid[index]:
                    continue
                # Exit at the next 1m open after the signal minute (executable).
                exec_idx = int(exit_idx[index]) + 1
                if exec_idx >= len(frame):
                    exec_idx = int(exit_idx[index])
                binance_exit = float(frame.open.to_numpy()[exec_idx])
                bybit_exit = float(frame.bybit_open.to_numpy()[exec_idx])
                binance_leg = (
                    (binance_exit / binance_entries[index] - 1.0) * direction
                )
                bybit_leg = (
                    (bybit_exit / bybit_entries[index] - 1.0) * -direction
                )
                pair_return_pct = (binance_leg + bybit_leg) * 100.0
                equity_pct = (
                    pair_return_pct * leverage_per_leg / 2.0 - cost_pct_equity
                )
                trades.append(
                    CandidateTrade(
                        symbol=symbol,
                        entry_time=int(times_array[mask][index]),
                        exit_time=int(
                            frame.open_time.to_numpy()[exec_idx]
                        ),
                        direction=int(direction),
                        entry_price=float(binance_entries[index]),
                        exit_price=float(binance_exit),
                        outcome=str(outcome[index]),
                        pnl_equity_pct=float(equity_pct),
                        hold_minutes=int(exec_idx - positions[index] + 1),
                    )
                )
    trades.sort(key=lambda trade: trade.entry_time)
    filtered: list[CandidateTrade] = []
    last_entry = -10**18
    for trade in trades:
        if trade.entry_time < last_entry + cooldown_min * MINUTE_MS:
            continue
        filtered.append(trade)
        last_entry = trade.entry_time
    return filtered


def metrics(taken: list[CandidateTrade]) -> dict[str, Any]:
    if not taken:
        return {"trades": 0}
    pnl = np.array([trade.pnl_equity_pct for trade in taken])
    wins = pnl[pnl > 0].sum()
    losses = -pnl[pnl < 0].sum()
    equity = np.concatenate([[14.0], 14.0 * np.cumprod(1.0 + pnl / 100.0)])
    drawdown = (np.maximum.accumulate(equity) - equity) / np.maximum.accumulate(equity)
    frame = pd.DataFrame([vars(trade) for trade in taken])
    year = pd.to_datetime(frame.exit_time, unit="ms", utc=True).dt.year
    year_pf: list[float] = []
    year_counts: list[int] = []
    for value in sorted(frame.assign(year=year).year.unique()):
        sub = frame.loc[year == value, "pnl_equity_pct"]
        sw = sub[sub > 0].sum()
        sl = -sub[sub < 0].sum()
        year_pf.append(round(sw / sl if sl else (999.0 if sw else 0.0), 3))
        year_counts.append(int(len(sub)))
    return {
        "trades": int(len(taken)),
        "win_rate_pct": round(float((pnl > 0).mean() * 100.0), 2),
        "profit_factor": round(
            float(wins / losses) if losses else (999.0 if wins else 0.0), 3
        ),
        "net_pct": round(float(pnl.sum()), 2),
        "mean_trade_pct": round(float(pnl.mean()), 4),
        "max_drawdown_pct": round(float(drawdown.max() * 100.0), 2),
        "final_equity": round(float(equity[-1]), 4),
        "year_pf": ";".join(str(value) for value in year_pf),
        "year_counts": ";".join(str(value) for value in year_counts),
        "revert_count": int(sum(1 for trade in taken if trade.outcome == "REVERT")),
        "stop_count": int(sum(1 for trade in taken if trade.outcome == "STOP")),
        "time_count": int(sum(1 for trade in taken if trade.outcome == "TIME")),
    }


def main() -> None:
    args = parse_args()
    premium = build_premium(args.binance, args.bybit)
    if not premium:
        raise SystemExit("no symbols with both Binance and Bybit data")
    start_ms = int(pd.Timestamp(args.start, tz="UTC").timestamp() * 1000)
    end_ms = int(pd.Timestamp(args.end, tz="UTC").timestamp() * 1000)
    signal_panel, grid = build_signal_panel(
        premium, args.decision_bar, start_ms, end_ms
    )
    premium_matrix, z_matrix, vol_matrix, symbols = build_signal_matrices(
        signal_panel, grid
    )
    if args.grid:
        rows: list[dict] = []
        for threshold, z_thr, delay, revert, stop, hold in itertools.product(
            (0.10, 0.20, 0.30),
            (2.0, 3.0),
            (0, 5),
            (0.3, 0.5, 0.7),
            (1.5, 2.0, 3.0),
            (30, 60, 120),
        ):
            events = select_events_fast(
                premium_matrix,
                z_matrix,
                vol_matrix,
                symbols,
                grid,
                threshold,
                z_thr,
                args.min_21d_volume_usdt,
            )
            trades = build_pair_trades(
                events,
                premium,
                delay,
                args.cooldown_min,
                args.leverage_per_leg,
                revert,
                stop,
                hold,
                args.binance_fee_bps,
                args.binance_slippage_bps,
                args.bybit_fee_bps,
                args.bybit_slippage_bps,
                args.min_entry_premium_pct,
                args.max_entry_premium_pct,
                args.bybit_entry_delay_min,
                args.cost_multiplier,
            )
            _, taken = simulate_equity(trades, args.initial_equity, args.max_trades)
            if not taken:
                continue
            metric = metrics(taken)
            rows.append(
                {
                    "threshold_pct": threshold,
                    "z_threshold": z_thr,
                    "entry_delay_min": delay,
                    "revert_ratio": revert,
                    "stop_ratio": stop,
                    "max_hold_minutes": hold,
                    "events": len(events),
                    **metric,
                }
            )
        result = pd.DataFrame(rows).sort_values(
            ["profit_factor", "net_pct"], ascending=False
        )
        output = args.output or (
            ROOT / "data" / "research" / "s0_cross_exchange_pair_dev_grid.csv"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(output, index=False)
        print(result.head(40).to_string(index=False), flush=True)
        return
    events = select_events_fast(
        premium_matrix,
        z_matrix,
        vol_matrix,
        symbols,
        grid,
        args.threshold_pct,
        args.z_threshold,
        args.min_21d_volume_usdt,
    )
    trades = build_pair_trades(
        events,
        premium,
        args.entry_delay_min,
        args.cooldown_min,
        args.leverage_per_leg,
        args.revert_ratio,
        args.stop_ratio,
        args.max_hold_minutes,
        args.binance_fee_bps,
        args.binance_slippage_bps,
        args.bybit_fee_bps,
        args.bybit_slippage_bps,
        args.min_entry_premium_pct,
        args.max_entry_premium_pct,
        args.bybit_entry_delay_min,
        args.cost_multiplier,
    )
    curve, taken = simulate_equity(trades, args.initial_equity, args.max_trades)
    result = metrics(taken)
    result["events"] = len(events)
    args.output.mkdir(parents=True, exist_ok=True)
    trades_frame = pd.DataFrame([vars(trade) for trade in taken])
    with gzip.open(args.output / "trades.csv.gz", "wt", encoding="utf-8") as handle:
        if not trades_frame.empty:
            trades_frame.to_csv(handle, index=False)
    curve.to_csv(args.output / "equity_curve.csv", index=False)
    (args.output / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
