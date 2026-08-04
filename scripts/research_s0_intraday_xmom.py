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

from scripts.research_s0_btc_impulse_alt import rolling_daily_volume  # noqa: E402
from scripts.research_s0_phase1_highlev_replay import (  # noqa: E402
    CandidateTrade,
    load_bars,
    simulate_equity,
    simulate_exit,
)

DEFAULT_DATA = ROOT / "data" / "research" / "binance_um_1m_cross_year" / "parquet"
DEFAULT_UNIVERSE = ROOT / "data" / "research" / "s0_public_1m" / "universe.json"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_intraday_xmom"
MINUTE_MS = 60_000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Intraday cross-sectional momentum: at every decision bar, rank liquid "
            "alts by their lookback return and buy the strongest one at the next 1m "
            "open; fast stop/TP/time exit; one position at a time with compounding."
        )
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--universe-json", type=Path, default=DEFAULT_UNIVERSE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default="2026-08-06")
    parser.add_argument("--decision-bar", type=int, default=15, help="minutes")
    parser.add_argument("--lookback", type=int, default=60, help="minutes")
    parser.add_argument("--min-ret-pct", type=float, default=0.3)
    parser.add_argument("--entry-delay-min", type=int, default=0)
    parser.add_argument("--cooldown-min", type=int, default=15)
    parser.add_argument("--min-21d-volume-usdt", type=float, default=20_000_000.0)
    parser.add_argument("--max-alts", type=int, default=30)
    parser.add_argument("--leverage", type=float, default=5.0)
    parser.add_argument("--risk-pct", type=float, default=30.0)
    parser.add_argument("--stop-pct", type=float, default=1.0)
    parser.add_argument("--tp-r", type=float, default=2.0)
    parser.add_argument("--max-hold-minutes", type=int, default=30)
    parser.add_argument("--fee-bps", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    parser.add_argument("--maintenance-margin-pct", type=float, default=0.5)
    parser.add_argument("--initial-equity", type=float, default=14.0)
    parser.add_argument("--allow-short", action="store_true")
    parser.add_argument(
        "--only-short",
        action="store_true",
        help="Reversal mode: always pick the weakest (most negative) liquid alt.",
    )
    parser.add_argument("--max-trades", type=int, default=0)
    return parser.parse_args()


def build_panel(
    data: Path,
    items: list[dict[str, Any]],
    max_alts: int,
    decision_bar: int,
    lookback: int,
    frame_cache: dict[str, pd.DataFrame] | None = None,
) -> tuple[
    dict[str, dict[str, np.ndarray]],
    list[int],
]:
    """Return per-symbol 1m arrays plus the UTC-aligned decision grid."""
    panel: dict[str, dict[str, np.ndarray]] = {}
    grid_min = 10**18
    grid_max = 0
    for item in items[:max_alts]:
        symbol = str(item["symbol"])
        if symbol == "BTCUSDT":
            continue
        path = data / f"{symbol}.parquet"
        if not path.exists():
            continue
        frame = frame_cache.get(symbol) if frame_cache is not None else None
        if frame is None:
            frame = load_bars(path)
            if frame_cache is not None:
                frame_cache[symbol] = frame
        if frame is None or frame.empty:
            continue
        day_idx, rv = rolling_daily_volume(frame)
        minute_times = frame.open_time.to_numpy(dtype="int64")
        minute_days = pd.Series(
            pd.to_datetime(minute_times, unit="ms", utc=True)
        ).dt.normalize()
        day_positions = np.searchsorted(
            day_idx, minute_days.astype("int64").to_numpy(), side="right"
        ) - 1
        day_positions = np.clip(day_positions, 0, len(day_idx) - 1)
        vol21 = np.where(
            np.isfinite(rv[day_positions]), rv[day_positions], np.nan
        )
        if len(minute_times) <= lookback // MINUTE_MS + 2:
            continue
        panel[symbol] = {
            "open_time": minute_times,
            "close": frame.close.to_numpy(dtype="float64"),
            "vol21": vol21,
            "onboard": np.int64(item.get("onboard_date", 0)),
        }
        grid_min = min(grid_min, int(minute_times.min()))
        grid_max = max(grid_max, int(minute_times.max()))
    if not panel:
        raise SystemExit("no usable alt panels")
    # UTC-aligned decision grid; each T is a minute boundary that has closed.
    grid_start = (grid_min // (decision_bar * MINUTE_MS) + 1) * (decision_bar * MINUTE_MS)
    grid = list(
        range(
            grid_start,
            grid_max + decision_bar * MINUTE_MS,
            decision_bar * MINUTE_MS,
        )
    )
    return panel, grid


def select_best(
    panel: dict[str, dict[str, np.ndarray]],
    grid: list[int],
    min_ret: float,
    min_volume: float,
    allow_short: bool,
    decision_bar: int,
    lookback: int,
    only_short: bool = False,
) -> list[tuple[int, str, int, float]]:
    """Return (decision_close_ms, symbol, direction, ret)."""
    candidates: list[tuple[int, str, int, float]] = []
    for decision_time in grid:
        best_long: str | None = None
        best_short: str | None = None
        best_long_ret = -10.0**9
        best_short_ret = 10.0**9
        for symbol, arrays in panel.items():
            times = arrays["open_time"]
            current_time = decision_time - MINUTE_MS
            position = int(np.searchsorted(times, decision_time, side="left")) - 1
            if position < 0 or times[position] != current_time:
                continue
            lag_close_time = current_time - lookback * MINUTE_MS
            lag_position = int(
                np.searchsorted(times, lag_close_time + MINUTE_MS, side="left")
            ) - 1
            if lag_position < 0 or times[lag_position] != lag_close_time:
                continue
            ret = float(arrays["close"][position] / arrays["close"][lag_position] - 1.0)
            vol = float(arrays["vol21"][position])
            if not np.isfinite(ret) or not np.isfinite(vol) or vol <= 0:
                continue
            if vol < min_volume or int(arrays["onboard"]) > decision_time:
                continue
            if only_short:
                if ret <= -min_ret and ret < best_short_ret:
                    best_short, best_short_ret = symbol, ret
            else:
                if ret >= min_ret and ret > best_long_ret:
                    best_long, best_long_ret = symbol, ret
                elif allow_short and ret <= -min_ret and ret < best_short_ret:
                    best_short, best_short_ret = symbol, ret
        if best_long is not None:
            candidates.append((decision_time, best_long, 1, best_long_ret))
        elif best_short is not None:
            candidates.append((decision_time, best_short, -1, best_short_ret))
    return candidates


def build_trades(
    candidates: list[tuple[int, str, int, float]],
    panel: dict[str, dict[str, np.ndarray]],
    data: Path,
    entry_delay_min: int,
    decision_bar: int,
    cooldown_min: int,
    leverage: float,
    risk_pct: float,
    stop_pct: float,
    tp_r: float,
    max_hold_minutes: int,
    fee_bps: float,
    slippage_bps: float,
    maintenance_margin_pct: float,
    minute_cache: dict[str, pd.DataFrame] | None = None,
) -> list[CandidateTrade]:
    effective_stop = min(stop_pct, risk_pct / leverage)
    liq_pct = 100.0 / leverage - maintenance_margin_pct
    cost_pct_equity = (fee_bps + slippage_bps) * 2.0 * leverage / 100.0
    trades: list[CandidateTrade] = []
    last_entry = -10**18
    minute_cache = minute_cache if minute_cache is not None else {}
    for decision_time, symbol, direction, _ret in candidates:
        entry_floor = decision_time + entry_delay_min * MINUTE_MS
        if entry_floor < last_entry + cooldown_min * MINUTE_MS:
            continue
        frame = minute_cache.get(symbol)
        if frame is None:
            frame = load_bars(data / f"{symbol}.parquet")
            if frame is None:
                continue
            minute_cache[symbol] = frame
        minute_times = frame.open_time.to_numpy()
        position = int(minute_times.searchsorted(entry_floor, side="left"))
        if position >= len(minute_times):
            continue
        entry_ms = int(minute_times[position])
        entry_price = float(frame.open.to_numpy()[position])
        if entry_price <= 0:
            continue
        if effective_stop >= liq_pct:
            outcome = "LIQUIDATED"
            exit_price = (
                entry_price * (1.0 - liq_pct / 100.0)
                if direction == 1
                else entry_price * (1.0 + liq_pct / 100.0)
            )
            pnl_pct = -100.0
            hold = 1
        else:
            result = simulate_exit(
                frame,
                entry_ms,
                entry_price,
                direction,
                effective_stop,
                tp_r,
                max_hold_minutes,
            )
            if result is None:
                continue
            exit_ms, exit_price, outcome, hold = result
            gross = (
                (exit_price / entry_price - 1.0) * 100.0 * leverage * direction
            )
            pnl_pct = gross - cost_pct_equity
        trades.append(
            CandidateTrade(
                symbol=symbol,
                entry_time=int(entry_ms),
                exit_time=int(exit_ms),
                direction=direction,
                entry_price=entry_price,
                exit_price=exit_price,
                outcome=outcome,
                pnl_equity_pct=float(pnl_pct),
                hold_minutes=int(hold),
            )
        )
        last_entry = entry_ms
    return trades


def simulate_exits_vectorized(
    frame: pd.DataFrame,
    entries: np.ndarray,
    entry_prices: np.ndarray,
    direction: int,
    stop_pct: float,
    tp_r: float,
    max_hold: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Batch exit simulation matching simulate_exit's adverse-first semantics."""
    entries = np.asarray(entries, dtype="int64")
    entry_prices = np.asarray(entry_prices, dtype="float64")
    count = len(entries)
    times = frame.open_time.to_numpy()
    high = frame.high.to_numpy()
    low = frame.low.to_numpy()
    close = frame.close.to_numpy()
    starts = np.searchsorted(times, entries, side="left")
    if direction == 1:
        stop = entry_prices * (1.0 - stop_pct / 100.0)
        target = entry_prices * (1.0 + stop_pct * tp_r / 100.0)
    else:
        stop = entry_prices * (1.0 + stop_pct / 100.0)
        target = entry_prices * (1.0 - stop_pct * tp_r / 100.0)
    exit_idx = np.full(count, -1, dtype="int64")
    exit_price = np.full(count, np.nan, dtype="float64")
    outcome = np.full(count, "", dtype=object)
    hold_minutes = np.zeros(count, dtype="int64")
    active = (starts < len(times)).copy()
    for step in range(0, max_hold + 1):
        idx = starts + step
        valid = active & (idx < len(times))
        if not valid.any():
            break
        if direction == 1:
            stop_hit = low[idx[valid]] <= stop[valid]
            tp_hit = high[idx[valid]] >= target[valid]
        else:
            stop_hit = high[idx[valid]] >= stop[valid]
            tp_hit = low[idx[valid]] <= target[valid]
        stop_mask = valid.copy()
        stop_mask[valid] = stop_hit
        if stop_mask.any():
            exit_idx[stop_mask] = idx[stop_mask]
            exit_price[stop_mask] = stop[stop_mask]
            outcome[stop_mask] = "STOP"
            hold_minutes[stop_mask] = step + 1
        tp_mask = valid.copy()
        tp_mask[valid] = tp_hit & ~stop_hit
        if tp_mask.any():
            exit_idx[tp_mask] = idx[tp_mask]
            exit_price[tp_mask] = target[tp_mask]
            outcome[tp_mask] = "TAKE_PROFIT"
            hold_minutes[tp_mask] = step + 1
        active[valid] = ~(stop_hit | tp_hit)
    remaining = active
    if remaining.any():
        end_idx = np.minimum(starts + max_hold, len(times) - 1)
        exit_idx[remaining] = end_idx[remaining]
        exit_price[remaining] = close[end_idx[remaining]]
        outcome[remaining] = "TIME"
        hold_minutes[remaining] = np.minimum(
            max_hold + 1, len(times) - starts[remaining]
        )
    exit_ms = np.where(exit_idx >= 0, times[np.maximum(exit_idx, 0)], -1)
    return exit_ms, exit_price, outcome, hold_minutes


def summarize(taken: list[CandidateTrade]) -> dict[str, Any]:
    if not taken:
        return {"trades": 0}
    pnl = np.array([trade.pnl_equity_pct for trade in taken])
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    return {
        "trades": int(len(taken)),
        "win_rate_pct": round(float((pnl > 0).mean() * 100.0), 4),
        "profit_factor": round(
            float(wins.sum() / abs(losses.sum()))
            if len(losses) and losses.sum()
            else (999.0 if len(wins) else 0.0),
            4,
        ),
        "total_pnl_equity_pct": round(float(pnl.sum()), 4),
        "mean_trade_pct": round(float(pnl.mean()), 4),
        "stop_count": int(sum(1 for trade in taken if trade.outcome == "STOP")),
        "take_profit_count": int(sum(1 for trade in taken if trade.outcome == "TAKE_PROFIT")),
        "time_count": int(sum(1 for trade in taken if trade.outcome == "TIME")),
    }


def main() -> None:
    args = parse_args()
    items = json.loads(args.universe_json.read_text(encoding="utf-8"))["symbols"]
    panel, grid = build_panel(
        args.data, items, args.max_alts, args.decision_bar, args.lookback
    )
    start_ms = int(pd.Timestamp(args.start, tz="UTC").timestamp() * 1000)
    end_ms = int(pd.Timestamp(args.end, tz="UTC").timestamp() * 1000)
    grid = [t for t in grid if start_ms <= t <= end_ms]
    selections = select_best(
        panel,
        grid,
        args.min_ret_pct / 100.0,
        args.min_21d_volume_usdt,
        args.allow_short or args.only_short,
        args.decision_bar,
        args.lookback,
        args.only_short,
    )
    trades = build_trades(
        selections,
        panel,
        args.data,
        args.entry_delay_min,
        args.decision_bar,
        args.cooldown_min,
        args.leverage,
        args.risk_pct,
        args.stop_pct,
        args.tp_r,
        args.max_hold_minutes,
        args.fee_bps,
        args.slippage_bps,
        args.maintenance_margin_pct,
    )
    curve, taken = simulate_equity(trades, args.initial_equity, args.max_trades)
    summary = summarize(taken)
    summary["selections"] = len(selections)
    args.output.mkdir(parents=True, exist_ok=True)
    trades_frame = pd.DataFrame([vars(trade) for trade in taken])
    with gzip.open(args.output / "trades.csv.gz", "wt", encoding="utf-8") as handle:
        if not trades_frame.empty:
            trades_frame.to_csv(handle, index=False)
    curve.to_csv(args.output / "equity_curve.csv", index=False)
    if not trades_frame.empty:
        year = pd.to_datetime(trades_frame.exit_time, unit="ms", utc=True).dt.year
        by_year = (
            trades_frame.assign(year=year)
            .groupby("year", sort=True)
            .apply(
                lambda frame: pd.DataFrame(
                    {
                        "trades": len(frame),
                        "win_rate_pct": round(
                            float((frame.pnl_equity_pct > 0).mean() * 100.0), 4
                        ),
                        "profit_factor": round(
                            float(
                                frame.pnl_equity_pct[frame.pnl_equity_pct > 0].sum()
                                / abs(
                                    frame.pnl_equity_pct[frame.pnl_equity_pct < 0].sum()
                                )
                            )
                            if (frame.pnl_equity_pct < 0).any()
                            else 999.0,
                            4,
                        ),
                        "net_pct": round(float(frame.pnl_equity_pct.sum()), 4),
                    },
                    index=[0],
                )
            )
            .reset_index()
        )
        by_year = by_year.drop(
            columns=[column for column in by_year.columns if column.startswith("level_")]
        )
        by_year.to_csv(args.output / "by_year.csv", index=False)
        summary["by_year"] = json.loads(by_year.to_json(orient="records"))
    (args.output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
