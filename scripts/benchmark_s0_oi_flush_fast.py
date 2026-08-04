from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_s0_oi_flush import (  # noqa: E402
    CANDIDATES,
    DEFAULT_SYMBOLS,
    merge_metrics,
    signals,
)
from scripts.research_s0_intraday_xmom import (  # noqa: E402
    MINUTE_MS,
    CandidateTrade,
    simulate_equity,
    simulate_exits_vectorized,
)
from scripts.research_s0_phase1_highlev_replay import load_bars  # noqa: E402

DEFAULT_METRICS = ROOT / "data" / "research" / "binance_um_metrics_1h"
DEFAULT_1M = ROOT / "data" / "research" / "binance_um_1m_cross_year" / "parquet"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "OI flush/build-up events with 1m fast exits instead of hourly holds. "
            "Development-period grid for the two strongest pre-registered setups."
        )
    )
    parser.add_argument("--metrics", type=Path, default=DEFAULT_METRICS)
    parser.add_argument("--data", type=Path, default=DEFAULT_1M)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--start", default="2022-01-01")
    parser.add_argument("--end", default="2024-01-01")
    parser.add_argument("--leverage", type=float, default=5.0)
    parser.add_argument("--risk-pct", type=float, default=30.0)
    parser.add_argument("--fee-bps", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    return parser.parse_args()


def fast_trades(
    signals_frame: pd.DataFrame,
    data: Path,
    delay: int,
    leverage: float,
    risk_pct: float,
    stop_pct: float,
    tp_r: float,
    max_hold: int,
    fee_bps: float,
    slippage_bps: float,
) -> list[CandidateTrade]:
    frame_cache: dict[str, pd.DataFrame] = {}
    grouped: dict[str, tuple[list[int], list[float], list[int]]] = {}
    for row in signals_frame.itertuples(index=False):
        symbol = str(row.symbol)
        frame = frame_cache.get(symbol)
        if frame is None:
            frame = load_bars(data / f"{symbol}.parquet")
            if frame is None:
                continue
            frame_cache[symbol] = frame
        entry_floor = int(row.available_ms) + delay * MINUTE_MS
        minute_times = frame.open_time.to_numpy()
        position = int(np.searchsorted(minute_times, entry_floor, side="left"))
        if position >= len(minute_times):
            continue
        entry_ms = int(minute_times[position])
        entry_price = float(frame.open.to_numpy()[position])
        if entry_price <= 0:
            continue
        entry_times, entry_prices, directions = grouped.setdefault(
            symbol, ([], [], [])
        )
        entry_times.append(entry_ms)
        entry_prices.append(entry_price)
        directions.append(int(row.direction))
    effective_stop = min(stop_pct, risk_pct / leverage)
    cost_pct_equity = (fee_bps + slippage_bps) * 2.0 * leverage / 100.0
    trades: list[CandidateTrade] = []
    for symbol, (entry_times, entry_prices, directions) in grouped.items():
        frame = frame_cache[symbol]
        times_array = np.asarray(entry_times, dtype="int64")
        prices_array = np.asarray(entry_prices, dtype="float64")
        dirs_array = np.asarray(directions, dtype="int64")
        for direction in (1, -1):
            mask = dirs_array == direction
            if not mask.any():
                continue
            exit_ms, exit_price, outcome, hold = simulate_exits_vectorized(
                frame,
                times_array[mask],
                prices_array[mask],
                int(direction),
                effective_stop,
                tp_r,
                max_hold,
            )
            for index in range(len(exit_ms)):
                if exit_ms[index] < 0:
                    continue
                gross = (
                    (exit_price[index] / prices_array[mask][index] - 1.0)
                    * 100.0
                    * leverage
                    * direction
                )
                trades.append(
                    CandidateTrade(
                        symbol=symbol,
                        entry_time=int(times_array[mask][index]),
                        exit_time=int(exit_ms[index]),
                        direction=int(direction),
                        entry_price=float(prices_array[mask][index]),
                        exit_price=float(exit_price[index]),
                        outcome=str(outcome[index]),
                        pnl_equity_pct=float(gross - cost_pct_equity),
                        hold_minutes=int(hold[index]),
                    )
                )
    trades.sort(key=lambda trade: trade.entry_time)
    return trades


def main() -> None:
    args = parse_args()
    panel = pd.concat(
        [merge_metrics(symbol, args.metrics) for symbol in DEFAULT_SYMBOLS],
        ignore_index=True,
    )
    names = {"build_breakout_strong", "flush_continuation_strong"}
    candidates = [candidate for candidate in CANDIDATES if candidate.name in names]
    rows: list[dict] = []
    for candidate in candidates:
        selected = signals(panel, candidate)
        start_ts = pd.Timestamp(args.start, tz="UTC")
        end_ts = pd.Timestamp(args.end, tz="UTC")
        selected = selected[
            pd.to_datetime(selected.available_ms, unit="ms", utc=True).between(
                start_ts, end_ts
            )
        ]
        for delay, stop, tp_r, hold in itertools.product(
            (0, 5), (0.5, 1.0, 1.5), (2.0, 3.0), (30, 60, 120)
        ):
            trades = fast_trades(
                selected,
                args.data,
                delay,
                args.leverage,
                args.risk_pct,
                stop,
                tp_r,
                hold,
                args.fee_bps,
                args.slippage_bps,
            )
            _, taken = simulate_equity(trades, 14.0, 0)
            if not taken:
                continue
            pnl = np.array([trade.pnl_equity_pct for trade in taken])
            wins = pnl[pnl > 0].sum()
            losses = -pnl[pnl < 0].sum()
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
            equity = np.concatenate([[14.0], 14.0 * np.cumprod(1.0 + pnl / 100.0)])
            drawdown = (np.maximum.accumulate(equity) - equity) / np.maximum.accumulate(
                equity
            )
            rows.append(
                {
                    "setup": candidate.name,
                    "entry_delay_min": delay,
                    "stop_pct": stop,
                    "tp_r": tp_r,
                    "max_hold_minutes": hold,
                    "signals": int(len(selected)),
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
                }
            )
    result = pd.DataFrame(rows).sort_values(
        ["profit_factor", "net_pct"], ascending=False
    )
    output = args.output or (
        ROOT / "data" / "research" / "s0_oi_flush_fast_dev_grid.csv"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output, index=False)
    print(result.head(40).to_string(index=False), flush=True)


if __name__ == "__main__":
    main()
