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

from scripts.research_s0_intraday_xmom import (  # noqa: E402
    MINUTE_MS,
    CandidateTrade,
    build_panel,
    simulate_equity,
    simulate_exits_vectorized,
)
from scripts.research_s0_phase1_highlev_replay import load_bars  # noqa: E402

DEFAULT_DATA = ROOT / "data" / "research" / "binance_um_1m_cross_year" / "parquet"
DEFAULT_UNIVERSE = ROOT / "data" / "research" / "s0_public_1m" / "universe.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Vectorized dev-period grid for intraday cross-sectional momentum."
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--universe-json", type=Path, default=DEFAULT_UNIVERSE)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default="2022-12-31")
    parser.add_argument("--max-alts", type=int, default=30)
    parser.add_argument("--leverage", type=float, default=5.0)
    parser.add_argument("--risk-pct", type=float, default=30.0)
    parser.add_argument("--fee-bps", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    parser.add_argument("--min-21d-volume-usdt", type=float, default=20_000_000.0)
    return parser.parse_args()


def build_ret_matrix(
    panel: dict[str, dict[str, np.ndarray]],
    grid: list[int],
    lookback: int,
) -> tuple[np.ndarray, list[str]]:
    symbols = list(panel.keys())
    count = len(grid)
    matrix = np.full((count, len(symbols)), np.nan)
    grid_array = np.asarray(grid, dtype="int64")
    current_time = grid_array - MINUTE_MS
    lag_time = current_time - lookback * MINUTE_MS
    for column, symbol in enumerate(symbols):
        arrays = panel[symbol]
        times = arrays["open_time"]
        closes = arrays["close"]
        current_positions = np.searchsorted(times, grid_array, side="left") - 1
        lag_positions = np.searchsorted(
            times, lag_time + MINUTE_MS, side="left"
        ) - 1
        valid = (
            (current_positions >= 0)
            & (lag_positions >= 0)
            & (times[np.maximum(current_positions, 0)] == current_time)
            & (times[np.maximum(lag_positions, 0)] == lag_time)
        )
        with np.errstate(invalid="ignore", divide="ignore"):
            ret = np.where(
                valid,
                closes[np.maximum(current_positions, 0)]
                / closes[np.maximum(lag_positions, 0)]
                - 1.0,
                np.nan,
            )
        matrix[:, column] = ret
    return matrix, symbols


def select_from_matrix(
    matrix: np.ndarray,
    symbols: list[str],
    grid: list[int],
    panel: dict[str, dict[str, np.ndarray]],
    min_ret: float,
    min_volume: float,
) -> list[tuple[int, str, int, float]]:
    with np.errstate(invalid="ignore"):
        masked = np.where(matrix >= min_ret, matrix, np.nan)
    has_any = np.any(np.isfinite(masked), axis=1)
    best_positions = np.full(len(grid), -1, dtype="int64")
    best_positions[has_any] = np.nanargmax(masked[has_any], axis=1)
    candidates: list[tuple[int, str, int, float]] = []
    for index, decision_time in enumerate(grid):
        column = int(best_positions[index])
        if column < 0:
            continue
        ret = float(matrix[index, column])
        if not np.isfinite(ret) or ret < min_ret:
            continue
        arrays = panel[symbols[column]]
        position = int(
            np.searchsorted(arrays["open_time"], decision_time, side="left")
        ) - 1
        if position < 0:
            continue
        if float(arrays["vol21"][position]) < min_volume:
            continue
        if int(arrays["onboard"]) > decision_time:
            continue
        candidates.append((decision_time, symbols[column], 1, ret))
    return candidates


def build_trades_batch(
    selections: list[tuple[int, str, int, float]],
    frame_cache: dict[str, pd.DataFrame],
    data: Path,
    entry_delay_min: int,
    cooldown_min: int,
    leverage: float,
    risk_pct: float,
    stop_pct: float,
    tp_r: float,
    max_hold: int,
    fee_bps: float,
    slippage_bps: float,
) -> list[CandidateTrade]:
    grouped: dict[str, tuple[list[int], list[float], list[int]]] = {}
    for decision_time, symbol, direction, _ret in selections:
        entry_floor = decision_time + entry_delay_min * MINUTE_MS
        frame = frame_cache.get(symbol)
        if frame is None:
            frame = load_bars(data / f"{symbol}.parquet")
            if frame is None:
                continue
            frame_cache[symbol] = frame
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
        directions.append(direction)
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
    filtered: list[CandidateTrade] = []
    last_entry = -10**18
    for trade in trades:
        if trade.entry_time < last_entry + cooldown_min * MINUTE_MS:
            continue
        filtered.append(trade)
        last_entry = trade.entry_time
    return filtered


def main() -> None:
    args = parse_args()
    items = json.loads(args.universe_json.read_text(encoding="utf-8"))["symbols"]
    start_ms = int(pd.Timestamp(args.start, tz="UTC").timestamp() * 1000)
    end_ms = int(pd.Timestamp(args.end, tz="UTC").timestamp() * 1000)
    frame_cache: dict[str, pd.DataFrame] = {}
    matrices: dict[int, tuple[np.ndarray, list[str], dict, list[int]]] = {}
    for lookback in (30, 60, 120):
        panel, grid = build_panel(
            args.data, items, args.max_alts, 15, lookback, frame_cache
        )
        grid = [t for t in grid if start_ms <= t <= end_ms]
        matrix, symbols = build_ret_matrix(panel, grid, lookback)
        matrices[lookback] = (matrix, symbols, panel, grid)
    rows: list[dict] = []
    for lookback, min_ret, delay, stop, tp_r, hold in itertools.product(
        (30, 60, 120),
        (0.5, 1.0, 2.0),
        (0, 5),
        (0.5, 1.0),
        (2.0, 3.0),
        (15, 30, 60),
    ):
        matrix, symbols, panel, grid = matrices[lookback]
        selections = select_from_matrix(
            matrix,
            symbols,
            grid,
            panel,
            min_ret / 100.0,
            args.min_21d_volume_usdt,
        )
        trades = build_trades_batch(
            selections,
            frame_cache,
            args.data,
            delay,
            15,
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
                "lookback": lookback,
                "min_ret_pct": min_ret,
                "entry_delay_min": delay,
                "stop_pct": stop,
                "tp_r": tp_r,
                "max_hold_minutes": hold,
                "selections": len(selections),
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
        ROOT / "data" / "research" / "s0_intraday_xmom_dev_grid.csv"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output, index=False)
    print(result.head(40).to_string(index=False), flush=True)


if __name__ == "__main__":
    main()
