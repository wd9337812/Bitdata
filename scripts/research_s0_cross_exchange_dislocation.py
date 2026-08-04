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

from scripts.research_s0_btc_impulse_alt import rolling_daily_volume  # noqa: E402
from scripts.research_s0_intraday_xmom import (  # noqa: E402
    MINUTE_MS,
    CandidateTrade,
    simulate_equity,
    simulate_exits_vectorized,
)
from scripts.research_s0_phase1_highlev_replay import load_bars  # noqa: E402

DEFAULT_BINANCE = ROOT / "data" / "research" / "binance_um_1m_cross_year" / "parquet"
DEFAULT_BYBIT = ROOT / "data" / "research" / "bybit_mt4_1m" / "parquet"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Cross-exchange dislocation events: when Binance perp price diverges "
            "from Bybit perp price beyond a point-in-time z/absolute threshold, fade "
            "the premium on Binance with fast 1m exits."
        )
    )
    parser.add_argument("--binance", type=Path, default=DEFAULT_BINANCE)
    parser.add_argument("--bybit", type=Path, default=DEFAULT_BYBIT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default="2026-08-06")
    parser.add_argument("--grid", action="store_true")
    parser.add_argument("--threshold-pct", type=float, default=0.10)
    parser.add_argument("--z-threshold", type=float, default=2.0)
    parser.add_argument("--decision-bar", type=int, default=5)
    parser.add_argument("--entry-delay-min", type=int, default=0)
    parser.add_argument("--min-21d-volume-usdt", type=float, default=20_000_000.0)
    parser.add_argument("--leverage", type=float, default=5.0)
    parser.add_argument("--risk-pct", type=float, default=30.0)
    parser.add_argument("--stop-pct", type=float, default=0.5)
    parser.add_argument("--tp-r", type=float, default=2.0)
    parser.add_argument("--max-hold-minutes", type=int, default=60)
    parser.add_argument("--cooldown-min", type=int, default=30)
    parser.add_argument(
        "--exit-mode",
        choices=("price", "revert"),
        default="price",
        help="price=stop/TP on Binance price; revert=exit when the dislocation reverts.",
    )
    parser.add_argument("--revert-ratio", type=float, default=0.5)
    parser.add_argument("--stop-ratio", type=float, default=2.0)
    parser.add_argument("--fee-bps", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    parser.add_argument("--initial-equity", type=float, default=14.0)
    parser.add_argument("--max-trades", type=int, default=0)
    return parser.parse_args()


def build_premium(
    binance_dir: Path,
    bybit_dir: Path,
) -> dict[str, pd.DataFrame]:
    premium: dict[str, pd.DataFrame] = {}
    for bybit_path in sorted(bybit_dir.glob("*.parquet")):
        symbol = bybit_path.stem
        binance_path = binance_dir / f"{symbol}.parquet"
        if not binance_path.exists():
            continue
        bybit = pd.read_parquet(
            bybit_path, columns=["open_time", "open", "high", "low", "close"]
        ).rename(
            columns={
                "open": "bybit_open",
                "high": "bybit_high",
                "low": "bybit_low",
                "close": "bybit_close",
            }
        )
        binance = pd.read_parquet(
            binance_path,
            columns=["open_time", "open", "high", "low", "close", "volume", "quote_volume"],
        )
        merged = binance.merge(
            bybit,
            on="open_time",
            how="inner",
        )
        if merged.empty:
            continue
        merged = merged.sort_values("open_time").reset_index(drop=True)
        merged["premium_pct"] = (merged.close / merged.bybit_close - 1.0) * 100.0
        day_idx, rv = rolling_daily_volume(merged)
        days = pd.Series(
            pd.to_datetime(merged.open_time, unit="ms", utc=True)
        ).dt.normalize()
        positions = np.clip(
            np.searchsorted(
                day_idx, days.astype("int64").to_numpy(), side="right"
            )
            - 1,
            0,
            len(day_idx) - 1,
        )
        merged["vol21"] = np.where(
            np.isfinite(rv[positions]), rv[positions], np.nan
        )
        premium[symbol] = merged
    return premium


def build_signal_panel(
    premium: dict[str, pd.DataFrame],
    decision_bar: int,
    start_ms: int,
    end_ms: int,
) -> tuple[dict[str, dict[str, np.ndarray]], list[int]]:
    step_ms = decision_bar * MINUTE_MS
    grid_start = (start_ms // step_ms + 1) * step_ms
    grid = list(range(grid_start, end_ms + step_ms, step_ms))
    per_symbol: dict[str, dict[str, np.ndarray]] = {}
    for symbol, frame in premium.items():
        frame = frame[
            (frame.open_time >= start_ms) & (frame.open_time <= end_ms)
        ]
        if frame.empty:
            continue
        bar = frame.open_time.to_numpy() // step_ms
        agg = (
            frame.assign(bar=bar)
            .groupby("bar", as_index=False)
            .agg(
                open_time=("open_time", "last"),
                premium_pct=("premium_pct", "last"),
                vol21=("vol21", "last"),
            )
            .sort_values("bar")
            .reset_index(drop=True)
        )
        premiums = agg.premium_pct.to_numpy(dtype="float64")
        rolling_mean = pd.Series(premiums).rolling(288, min_periods=60).mean().shift(1).to_numpy()
        rolling_std = pd.Series(premiums).rolling(288, min_periods=60).std().shift(1).to_numpy()
        with np.errstate(invalid="ignore", divide="ignore"):
            z = (premiums - rolling_mean) / rolling_std
        per_symbol[symbol] = {
            "open_time": agg.open_time.to_numpy(dtype="int64"),
            "premium": premiums,
            "z": z,
            "vol21": agg.vol21.to_numpy(dtype="float64"),
        }
    return per_symbol, grid


def select_events(
    per_symbol: dict[str, dict[str, np.ndarray]],
    grid: list[int],
    threshold_pct: float,
    z_threshold: float,
    min_volume: float,
) -> list[tuple[int, str, int, float]]:
    """Return (signal_close_ms, symbol, direction, premium)."""
    events: list[tuple[int, str, int, float]] = []
    for signal_time in grid:
        best_symbol: str | None = None
        best_premium = -1.0
        best_direction = 0
        for symbol, arrays in per_symbol.items():
            times = arrays["open_time"]
            position = int(np.searchsorted(times, signal_time, side="left")) - 1
            if position < 0 or times[position] != signal_time - MINUTE_MS:
                continue
            premium_value = float(arrays["premium"][position])
            z_value = float(arrays["z"][position])
            volume = float(arrays["vol21"][position])
            if not np.isfinite(premium_value) or not np.isfinite(z_value):
                continue
            if volume < min_volume:
                continue
            if abs(premium_value) < threshold_pct or abs(z_value) < z_threshold:
                continue
            if abs(premium_value) > best_premium:
                best_symbol = symbol
                best_premium = abs(premium_value)
                best_direction = -1 if premium_value > 0 else 1
        if best_symbol is not None:
            events.append((signal_time, best_symbol, best_direction, best_premium))
    return events


def build_signal_matrices(
    per_symbol: dict[str, dict[str, np.ndarray]],
    grid: list[int],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    symbols = list(per_symbol.keys())
    count = len(grid)
    grid_array = np.asarray(grid, dtype="int64")
    premium_matrix = np.full((count, len(symbols)), np.nan)
    z_matrix = np.full((count, len(symbols)), np.nan)
    vol_matrix = np.full((count, len(symbols)), np.nan)
    for column, symbol in enumerate(symbols):
        arrays = per_symbol[symbol]
        times = arrays["open_time"]
        positions = np.searchsorted(times, grid_array, side="left") - 1
        valid = (
            (positions >= 0)
            & (times[np.maximum(positions, 0)] == grid_array - MINUTE_MS)
        )
        safe = np.maximum(positions, 0)
        premium_matrix[:, column] = np.where(
            valid, arrays["premium"][safe], np.nan
        )
        z_matrix[:, column] = np.where(valid, arrays["z"][safe], np.nan)
        vol_matrix[:, column] = np.where(valid, arrays["vol21"][safe], np.nan)
    return premium_matrix, z_matrix, vol_matrix, symbols


def select_events_fast(
    premium_matrix: np.ndarray,
    z_matrix: np.ndarray,
    vol_matrix: np.ndarray,
    symbols: list[str],
    grid: list[int],
    threshold_pct: float,
    z_threshold: float,
    min_volume: float,
) -> list[tuple[int, str, int, float]]:
    with np.errstate(invalid="ignore"):
        mask = (
            (np.abs(premium_matrix) >= threshold_pct)
            & (np.abs(z_matrix) >= z_threshold)
            & (vol_matrix >= min_volume)
        )
        abs_premium = np.where(mask, np.abs(premium_matrix), np.nan)
    has_any = np.any(np.isfinite(abs_premium), axis=1)
    best_columns = np.full(len(grid), -1, dtype="int64")
    best_columns[has_any] = np.nanargmax(abs_premium[has_any], axis=1)
    events: list[tuple[int, str, int, float]] = []
    for index, signal_time in enumerate(grid):
        column = int(best_columns[index])
        if column < 0:
            continue
        premium_value = float(premium_matrix[index, column])
        events.append(
            (
                signal_time,
                symbols[column],
                -1 if premium_value > 0 else 1,
                abs(premium_value),
            )
        )
    return events


def build_trades(
    events: list[tuple[int, str, int, float]],
    premium: dict[str, pd.DataFrame],
    delay: int,
    cooldown_min: int,
    leverage: float,
    risk_pct: float,
    stop_pct: float,
    tp_r: float,
    max_hold: int,
    fee_bps: float,
    slippage_bps: float,
    exit_mode: str = "price",
    revert_ratio: float = 0.5,
    stop_ratio: float = 2.0,
) -> list[CandidateTrade]:
    grouped: dict[
        str, tuple[list[int], list[float], list[int], list[float]]
    ] = {}
    for signal_time, symbol, direction, premium_value in events:
        frame = premium[symbol]
        entry_floor = signal_time + delay * MINUTE_MS
        minute_times = frame.open_time.to_numpy()
        position = int(np.searchsorted(minute_times, entry_floor, side="left"))
        if position >= len(minute_times):
            continue
        entry_ms = int(minute_times[position])
        entry_price = float(frame.open.to_numpy()[position])
        if entry_price <= 0:
            continue
        entry_times, entry_prices, directions, entry_premiums = grouped.setdefault(
            symbol, ([], [], [], [])
        )
        entry_times.append(entry_ms)
        entry_prices.append(entry_price)
        directions.append(direction)
        entry_premiums.append(
            -premium_value if direction == 1 else premium_value
        )
    effective_stop = min(stop_pct, risk_pct / leverage)
    cost_pct_equity = (fee_bps + slippage_bps) * 2.0 * leverage / 100.0
    trades: list[CandidateTrade] = []
    for symbol, (entry_times, entry_prices, directions, entry_premiums) in grouped.items():
        frame = premium[symbol]
        times_array = np.asarray(entry_times, dtype="int64")
        prices_array = np.asarray(entry_prices, dtype="float64")
        dirs_array = np.asarray(directions, dtype="int64")
        premium_array = np.asarray(entry_premiums, dtype="float64")
        for direction in (1, -1):
            mask = dirs_array == direction
            if not mask.any():
                continue
            if exit_mode == "revert":
                for index in range(int(mask.sum())):
                    original_index = int(np.flatnonzero(mask)[index])
                    result = simulate_premium_revert(
                        frame,
                        int(times_array[original_index]),
                        float(premium_array[original_index]),
                        int(direction),
                        revert_ratio,
                        stop_ratio,
                        max_hold,
                    )
                    if result is None:
                        continue
                    exit_ms_value, exit_price_value, outcome_value, hold_value = result
                    gross = (
                        (exit_price_value / prices_array[original_index] - 1.0)
                        * 100.0
                        * leverage
                        * direction
                    )
                    trades.append(
                        CandidateTrade(
                            symbol=symbol,
                            entry_time=int(times_array[original_index]),
                            exit_time=int(exit_ms_value),
                            direction=int(direction),
                            entry_price=float(prices_array[original_index]),
                            exit_price=float(exit_price_value),
                            outcome=str(outcome_value),
                            pnl_equity_pct=float(gross - cost_pct_equity),
                            hold_minutes=int(hold_value),
                        )
                    )
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


def simulate_premium_revert(
    frame: pd.DataFrame,
    entry_ms: int,
    entry_premium: float,
    direction: int,
    revert_ratio: float,
    stop_ratio: float,
    max_hold: int,
) -> tuple[int, float, str, int] | None:
    """Exit when the premium reverts (target) or widens (stop); adverse-first."""
    times = frame.open_time.to_numpy()
    premiums = frame.premium_pct.to_numpy()
    closes = frame.close.to_numpy()
    start = int(np.searchsorted(times, entry_ms, side="left"))
    if start >= len(times):
        return None
    end = min(start + max_hold + 1, len(times))
    target = entry_premium * revert_ratio
    stop = entry_premium * stop_ratio
    for index in range(start, end):
        premium_value = float(premiums[index])
        if direction == 1:
            if premium_value <= stop:
                return (
                    int(times[index]),
                    float(closes[index]),
                    "STOP",
                    index - start + 1,
                )
            if premium_value >= target:
                return (
                    int(times[index]),
                    float(closes[index]),
                    "REVERT",
                    index - start + 1,
                )
        else:
            if premium_value >= stop:
                return (
                    int(times[index]),
                    float(closes[index]),
                    "STOP",
                    index - start + 1,
                )
            if premium_value <= target:
                return (
                    int(times[index]),
                    float(closes[index]),
                    "REVERT",
                    index - start + 1,
                )
    last = end - 1
    return int(times[last]), float(closes[last]), "TIME", end - start


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
        "stop_count": int(sum(1 for trade in taken if trade.outcome == "STOP")),
        "take_profit_count": int(
            sum(1 for trade in taken if trade.outcome == "TAKE_PROFIT")
        ),
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
        for threshold, z_thr, delay, stop, tp_r, hold in itertools.product(
            (0.05, 0.10, 0.15),
            (2.0, 3.0),
            (0, 5),
            (0.3, 0.5, 1.0),
            (1.0, 2.0, 3.0),
            (15, 30, 60),
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
            trades = build_trades(
                events,
                premium,
                delay,
                args.cooldown_min,
                args.leverage,
                args.risk_pct,
                stop,
                tp_r,
                hold,
                args.fee_bps,
                args.slippage_bps,
                args.exit_mode,
                args.revert_ratio,
                args.stop_ratio,
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
                    "stop_pct": stop,
                    "tp_r": tp_r,
                    "max_hold_minutes": hold,
                    "events": len(events),
                    **metric,
                }
            )
        result = pd.DataFrame(rows).sort_values(
            ["profit_factor", "net_pct"], ascending=False
        )
        output = args.output or (
            ROOT / "data" / "research" / "s0_cross_exchange_dev_grid.csv"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(output, index=False)
        print(result.head(40).to_string(index=False), flush=True)
        return
    events = select_events(
        signal_panel,
        grid,
        args.threshold_pct,
        args.z_threshold,
        args.min_21d_volume_usdt,
    )
    trades = build_trades(
        events,
        premium,
        args.entry_delay_min,
        args.cooldown_min,
        args.leverage,
        args.risk_pct,
        args.stop_pct,
        args.tp_r,
        args.max_hold_minutes,
        args.fee_bps,
        args.slippage_bps,
        args.exit_mode,
        args.revert_ratio,
        args.stop_ratio,
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
