from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.research_s0_cross_exchange_pair import (  # noqa: E402
    simulate_premium_reverts_vectorized,
)
from scripts.research_s0_intraday_xmom import (  # noqa: E402
    CandidateTrade,
    simulate_equity,
)

DEFAULT_BINANCE = ROOT / "data" / "research" / "binance_um_aggtrades_10s"
DEFAULT_BYBIT = ROOT / "data" / "research" / "bybit_trades_10s" / "parquet"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sub-minute (10s) two-leg cross-exchange dislocation backtest on "
            "Binance aggTrades and Bybit official trade data."
        )
    )
    parser.add_argument("--binance", type=Path, default=DEFAULT_BINANCE)
    parser.add_argument("--bybit", type=Path, default=DEFAULT_BYBIT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--symbols", nargs="+", default=["SOLUSDT", "DOGEUSDT", "ADAUSDT"])
    parser.add_argument("--start", default="2026-06-01")
    parser.add_argument("--end", default="2026-06-30")
    parser.add_argument("--threshold-pct", type=float, default=0.20)
    parser.add_argument("--z-threshold", type=float, default=3.0)
    parser.add_argument("--z-window-bars", type=int, default=360)
    parser.add_argument("--min-entry-premium-pct", type=float, default=0.05)
    parser.add_argument("--max-entry-premium-pct", type=float, default=1.0)
    parser.add_argument("--revert-ratio", type=float, default=0.5)
    parser.add_argument("--stop-ratio", type=float, default=2.0)
    parser.add_argument("--max-hold-bars", type=int, default=360)
    parser.add_argument("--cooldown-bars", type=int, default=6)
    parser.add_argument("--bybit-entry-delay-bars", type=int, default=0)
    parser.add_argument("--leverage-per-leg", type=float, default=5.0)
    parser.add_argument("--binance-fee-bps", type=float, default=5.0)
    parser.add_argument("--binance-slippage-bps", type=float, default=2.0)
    parser.add_argument("--bybit-fee-bps", type=float, default=5.5)
    parser.add_argument("--bybit-slippage-bps", type=float, default=2.0)
    parser.add_argument("--cost-multiplier", type=float, default=1.0)
    parser.add_argument("--initial-equity", type=float, default=14.0)
    return parser.parse_args()


def load_10s(symbol: str, binance: Path, bybit: Path, start: str, end: str) -> pd.DataFrame | None:
    days = [
        stamp.strftime("%Y-%m-%d")
        for stamp in pd.date_range(start, end, freq="D")
    ]
    bin_parts: list[pd.DataFrame] = []
    by_parts: list[pd.DataFrame] = []
    for day in days:
        b = binance / symbol / f"{day}.parquet"
        y = bybit / symbol / f"{day}.parquet"
        if b.exists():
            bin_parts.append(
                pd.read_parquet(b, columns=["bin_ms", "open", "close"])
            )
        if y.exists():
            by_parts.append(
                pd.read_parquet(
                    y, columns=["bin_ms", "open", "close"]
                ).rename(columns={"open": "bybit_open", "close": "bybit_close"})
            )
    if not bin_parts or not by_parts:
        return None
    binance_frame = pd.concat(bin_parts, ignore_index=True)
    bybit_frame = pd.concat(by_parts, ignore_index=True)
    merged = binance_frame.merge(bybit_frame, on="bin_ms", how="inner")
    if merged.empty:
        return None
    merged = merged.sort_values("bin_ms").reset_index(drop=True)
    merged["premium_pct"] = (merged.close / merged.bybit_close - 1.0) * 100.0
    merged["open_time"] = merged.bin_ms
    return merged


def select_events(
    frame: pd.DataFrame,
    threshold_pct: float,
    z_threshold: float,
    z_window_bars: int,
) -> list[tuple[int, str, int, float]]:
    premiums = frame.premium_pct.to_numpy(dtype="float64")
    rolling_mean = (
        pd.Series(premiums).rolling(z_window_bars, min_periods=60).mean().shift(1).to_numpy()
    )
    rolling_std = (
        pd.Series(premiums).rolling(z_window_bars, min_periods=60).std().shift(1).to_numpy()
    )
    with np.errstate(invalid="ignore", divide="ignore"):
        z = (premiums - rolling_mean) / rolling_std
    events: list[tuple[int, str, int, float]] = []
    for index in range(len(frame)):
        premium_value = float(premiums[index])
        z_value = float(z[index])
        if not np.isfinite(premium_value) or not np.isfinite(z_value):
            continue
        if abs(premium_value) < threshold_pct or abs(z_value) < z_threshold:
            continue
        direction = -1 if premium_value > 0 else 1
        events.append(
            (
                int(frame.bin_ms.iloc[index]),
                str(frame.symbol.iloc[0]) if "symbol" in frame else "",
                direction,
                abs(premium_value),
            )
        )
    return events


def build_trades(
    events: list[tuple[int, str, int, float]],
    frame: pd.DataFrame,
    symbol: str,
    min_entry_premium_pct: float,
    max_entry_premium_pct: float,
    bybit_delay_bars: int,
    leverage_per_leg: float,
    revert_ratio: float,
    stop_ratio: float,
    max_hold_bars: int,
    cooldown_bars: int,
    binance_fee_bps: float,
    binance_slippage_bps: float,
    bybit_fee_bps: float,
    bybit_slippage_bps: float,
    cost_multiplier: float,
) -> list[CandidateTrade]:
    times = frame.bin_ms.to_numpy(dtype="int64")
    entries: list[tuple[int, int]] = []
    for signal_time, _, direction, _premium_value in events:
        position = int(np.searchsorted(times, signal_time, side="left"))
        if position + 1 >= len(times):
            continue
        entry_index = position + 1
        entry_premium = float(frame.premium_pct.to_numpy()[entry_index])
        if not np.isfinite(entry_premium):
            continue
        if direction == 1 and not (entry_premium <= -min_entry_premium_pct):
            continue
        if direction == -1 and not (entry_premium >= min_entry_premium_pct):
            continue
        if abs(entry_premium) > max_entry_premium_pct:
            continue
        if entry_index + bybit_delay_bars >= len(times):
            continue
        entries.append((entry_index, direction))
    if not entries:
        return []
    entry_indices = np.array([item[0] for item in entries], dtype="int64")
    directions = np.array([item[1] for item in entries], dtype="int64")
    entry_premiums = frame.premium_pct.to_numpy()[entry_indices]
    cost_pct_equity = (
        (binance_fee_bps + binance_slippage_bps + bybit_fee_bps + bybit_slippage_bps)
        / 10000.0
        * leverage_per_leg
        / 2.0
        * 100.0
        * cost_multiplier
    )
    trades: list[CandidateTrade] = []
    for direction in (1, -1):
        mask = directions == direction
        if not mask.any():
            continue
        exit_idx, outcome, hold, _ = simulate_premium_reverts_vectorized(
            frame,
            times[entry_indices[mask]],
            entry_premiums[mask],
            int(direction),
            revert_ratio,
            stop_ratio,
            max_hold_bars,
        )
        binance_entries = frame.open.to_numpy()[entry_indices[mask]]
        bybit_entry_positions = entry_indices[mask] + bybit_delay_bars
        bybit_entries = frame.bybit_open.to_numpy()[bybit_entry_positions]
        for index in range(len(exit_idx)):
            if exit_idx[index] < 0:
                continue
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
                    entry_time=int(times[entry_indices[mask][index]]),
                    exit_time=int(times[exec_idx]),
                    direction=int(direction),
                    entry_price=float(binance_entries[index]),
                    exit_price=float(binance_exit),
                    outcome=str(outcome[index]),
                    pnl_equity_pct=float(equity_pct),
                    hold_minutes=int(exec_idx - entry_indices[mask][index] + 1),
                )
            )
    trades.sort(key=lambda trade: trade.entry_time)
    filtered: list[CandidateTrade] = []
    last_entry = -10**18
    for trade in trades:
        if trade.entry_time < last_entry + cooldown_bars * 10_000:
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
        "revert_count": int(sum(1 for trade in taken if trade.outcome == "REVERT")),
        "stop_count": int(sum(1 for trade in taken if trade.outcome == "STOP")),
        "time_count": int(sum(1 for trade in taken if trade.outcome == "TIME")),
    }


def main() -> None:
    args = parse_args()
    all_trades: list[CandidateTrade] = []
    for symbol in args.symbols:
        frame = load_10s(symbol, args.binance, args.bybit, args.start, args.end)
        if frame is None or frame.empty:
            continue
        events = select_events(
            frame, args.threshold_pct, args.z_threshold, args.z_window_bars
        )
        trades = build_trades(
            events,
            frame,
            symbol,
            args.min_entry_premium_pct,
            args.max_entry_premium_pct,
            args.bybit_entry_delay_bars,
            args.leverage_per_leg,
            args.revert_ratio,
            args.stop_ratio,
            args.max_hold_bars,
            args.cooldown_bars,
            args.binance_fee_bps,
            args.binance_slippage_bps,
            args.bybit_fee_bps,
            args.bybit_slippage_bps,
            args.cost_multiplier,
        )
        all_trades.extend(trades)
    curve, taken = simulate_equity(all_trades, args.initial_equity, 0)
    result = metrics(taken)
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
