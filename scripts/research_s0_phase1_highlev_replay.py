from __future__ import annotations

import argparse
import gzip
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data" / "research" / "binance_um_1m_cross_year" / "parquet"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_phase1_highlev_replay"
MINUTE_MS = 60_000
RULES = ("breakout", "pullback", "vol_shock", "range_break")


@dataclass(frozen=True)
class CandidateTrade:
    symbol: str
    entry_time: int
    exit_time: int
    direction: int
    entry_price: float
    exit_price: float
    outcome: str
    pnl_equity_pct: float
    hold_minutes: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Account-level replay of full-margin high-leverage short-term "
            "strategies on 1m klines. Signals use only completed bars; entry "
            "happens at the next 1m open; exits use adverse-first ordering; "
            "one position at a time with compounding equity."
        )
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--universe-json", type=Path, default=None)
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default="2026-08-06")
    parser.add_argument("--rule", default="breakout", choices=RULES)
    parser.add_argument("--decision-bar", type=int, default=15, help="minutes")
    parser.add_argument("--lookback-bars", type=int, default=20)
    parser.add_argument("--min-gap-bars", type=int, default=4)
    parser.add_argument("--leverage", type=float, default=5.0)
    parser.add_argument("--risk-pct", type=float, default=30.0)
    parser.add_argument("--stop-pct", type=float, default=2.0)
    parser.add_argument("--tp-r", type=float, default=1.0)
    parser.add_argument("--max-hold-minutes", type=int, default=120)
    parser.add_argument("--fee-bps", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    parser.add_argument("--maintenance-margin-pct", type=float, default=0.5)
    parser.add_argument("--initial-equity", type=float, default=14.0)
    parser.add_argument("--allow-short", action="store_true")
    parser.add_argument("--vol-z-threshold", type=float, default=2.0)
    parser.add_argument("--max-trades", type=int, default=0)
    return parser.parse_args()


def load_bars(path: Path) -> pd.DataFrame | None:
    frame = pd.read_parquet(path)
    needed = {"open_time", "open", "high", "low", "close", "volume"}
    if not needed.issubset(frame.columns):
        return None
    keep = ["open_time", "open", "high", "low", "close", "volume"]
    if "quote_volume" in frame.columns:
        keep.append("quote_volume")
    for column in ("taker_buy_volume", "taker_buy_quote_volume"):
        if column in frame.columns:
            keep.append(column)
    frame = frame[keep].copy()
    frame = frame.sort_values("open_time").reset_index(drop=True)
    frame["open_time"] = frame["open_time"].astype("int64")
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if "quote_volume" not in frame.columns:
        frame["quote_volume"] = frame.volume * frame.close
    else:
        frame["quote_volume"] = pd.to_numeric(
            frame["quote_volume"], errors="coerce"
        )
    for column in ("taker_buy_volume", "taker_buy_quote_volume"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna(subset=["open", "high", "low", "close"]).reset_index(
        drop=True
    )


def resample_bars(frame: pd.DataFrame, decision_minutes: int) -> pd.DataFrame:
    frame = frame.copy()
    frame["bar"] = frame.open_time // (decision_minutes * MINUTE_MS)
    agg = (
        frame.groupby("bar", as_index=False)
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
            open_time=("open_time", "first"),
        )
        .sort_values("bar")
        .reset_index(drop=True)
    )
    return agg[["open_time", "open", "high", "low", "close", "volume"]]


def add_features(bars: pd.DataFrame, lookback: int) -> pd.DataFrame:
    out = bars.copy()
    out["ret_1"] = out.close.pct_change()
    out["ret_k"] = out.close.pct_change(lookback)
    out["sma"] = out.close.rolling(lookback).mean()
    out["high_max"] = out.high.rolling(lookback).max().shift(1)
    out["low_min"] = out.low.rolling(lookback).min().shift(1)
    vol_mean = out.volume.rolling(max(8, lookback)).mean()
    vol_std = out.volume.rolling(max(8, lookback)).std()
    out["vol_z"] = (out.volume - vol_mean) / vol_std.replace(0.0, np.nan)
    return out


def _long_signal(row: pd.Series, rule: str, vol_z_threshold: float) -> bool:
    if rule == "breakout":
        return bool(row.close > row.high_max)
    if rule == "pullback":
        return bool(row.close > row.sma and row.close < row.close_prev and row.low > row.low_min)
    if rule == "vol_shock":
        return bool(row.vol_z > vol_z_threshold and row.ret_1 > 0)
    if rule == "range_break":
        return bool(row.high > row.high_max and row.close > row.open)
    return False


def _short_signal(row: pd.Series, rule: str, vol_z_threshold: float) -> bool:
    if rule == "breakout":
        return bool(row.close < row.low_min)
    if rule == "pullback":
        return bool(row.close < row.sma and row.close > row.close_prev and row.high < row.high_max)
    if rule == "vol_shock":
        return bool(row.vol_z > vol_z_threshold and row.ret_1 < 0)
    if rule == "range_break":
        return bool(row.low < row.low_min and row.close < row.open)
    return False


def generate_candidates(
    frame: pd.DataFrame,
    decision_minutes: int,
    lookback: int,
    rule: str,
    min_gap_bars: int,
    allow_short: bool,
    vol_z_threshold: float,
) -> list[tuple[int, int, int, int]]:
    """Return (signal_ms, direction, bar_index, next_entry_ms)."""
    bars = add_features(resample_bars(frame, decision_minutes), lookback)
    if len(bars) < lookback + 4:
        return []
    bars["close_prev"] = bars.close.shift(1)
    candidates: list[tuple[int, int, int, int]] = []
    last_signal_bar = -10**9
    minute_times = frame.open_time.to_numpy()
    for index in range(lookback + 1, len(bars)):
        if index - last_signal_bar < min_gap_bars:
            continue
        row = bars.iloc[index]
        direction = 0
        if _long_signal(row, rule, vol_z_threshold):
            direction = 1
        elif allow_short and _short_signal(row, rule, vol_z_threshold):
            direction = -1
        if direction == 0:
            continue
        signal_ms = int(row.open_time) + decision_minutes * MINUTE_MS
        position = int(minute_times.searchsorted(signal_ms, side="left"))
        if position >= len(minute_times):
            continue
        entry_ms = int(minute_times[position])
        candidates.append((signal_ms, direction, index, entry_ms))
        last_signal_bar = index
    return candidates


def simulate_exit(
    frame: pd.DataFrame,
    entry_ms: int,
    entry_price: float,
    direction: int,
    stop_pct: float,
    tp_r: float,
    max_hold_minutes: int,
) -> tuple[int, float, str, int] | None:
    """Return (exit_ms, exit_price, outcome, hold_minutes) or None."""
    minute_times = frame.open_time.to_numpy()
    start = int(minute_times.searchsorted(entry_ms, side="left"))
    end = start + max_hold_minutes + 1
    if start >= len(minute_times):
        return None
    high = frame.high.to_numpy()[start:end]
    low = frame.low.to_numpy()[start:end]
    close = frame.close.to_numpy()[start:end]
    times = minute_times[start:end]
    stop = entry_price * (1.0 - stop_pct / 100.0) if direction == 1 else entry_price * (1.0 + stop_pct / 100.0)
    target = entry_price * (1.0 + stop_pct * tp_r / 100.0) if direction == 1 else entry_price * (1.0 - stop_pct * tp_r / 100.0)
    for index in range(len(times)):
        if direction == 1:
            stop_hit = low[index] <= stop
            target_hit = high[index] >= target
            # Adverse-first: if both are touched in the same bar, assume stop.
            if stop_hit:
                return int(times[index]), stop, "STOP", index + 1
            if target_hit:
                return int(times[index]), target, "TAKE_PROFIT", index + 1
        else:
            stop_hit = high[index] >= stop
            target_hit = low[index] <= target
            if stop_hit:
                return int(times[index]), stop, "STOP", index + 1
            if target_hit:
                return int(times[index]), target, "TAKE_PROFIT", index + 1
    if len(times) == 0:
        return None
    last_price = float(close[-1])
    return int(times[-1]), last_price, "TIME", len(times)


def build_trades(
    frame: pd.DataFrame,
    candidates: list[tuple[int, int, int, int]],
    leverage: float,
    risk_pct: float,
    stop_pct: float,
    tp_r: float,
    max_hold_minutes: int,
    fee_bps: float,
    slippage_bps: float,
    maintenance_margin_pct: float,
) -> list[CandidateTrade]:
    effective_stop = min(stop_pct, risk_pct / leverage)
    liq_pct = 100.0 / leverage - maintenance_margin_pct
    cost_pct_equity = (
        (fee_bps + slippage_bps) * 2.0 * leverage / 100.0
    )
    trades: list[CandidateTrade] = []
    minute_times = frame.open_time.to_numpy()
    open_prices = frame.open.to_numpy()
    for signal_ms, direction, _bar, entry_ms in candidates:
        position = int(minute_times.searchsorted(entry_ms, side="left"))
        if position >= len(minute_times):
            continue
        entry_price = float(open_prices[position])
        if entry_price <= 0:
            continue
        if effective_stop >= liq_pct:
            # Liquidation would occur before the configured stop.
            outcome = "LIQUIDATED"
            exit_price = entry_price * (1.0 - liq_pct / 100.0) if direction == 1 else entry_price * (1.0 + liq_pct / 100.0)
            exit_ms = int(minute_times[position])
            pnl_pct = -100.0
            hold = 1
        else:
            exit_result = simulate_exit(
                frame,
                entry_ms,
                entry_price,
                direction,
                effective_stop,
                tp_r,
                max_hold_minutes,
            )
            if exit_result is None:
                continue
            exit_ms, exit_price, outcome, hold = exit_result
            gross_equity_pct = (
                (exit_price / entry_price - 1.0) * 100.0 * leverage * direction
            )
            pnl_pct = gross_equity_pct - cost_pct_equity
        trades.append(
            CandidateTrade(
                symbol=str(frame.attrs.get("symbol", "")),
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
    return trades


def simulate_equity(
    trades: list[CandidateTrade],
    initial_equity: float,
    max_trades: int,
) -> tuple[pd.DataFrame, list[CandidateTrade]]:
    ordered = sorted(trades, key=lambda item: (item.entry_time, item.symbol))
    equity = initial_equity
    curve: list[dict[str, Any]] = [{"time": 0, "equity": equity, "trade_id": 0}]
    taken: list[CandidateTrade] = []
    busy_until = 0
    for index, trade in enumerate(ordered):
        if trade.entry_time < busy_until:
            continue
        equity *= 1.0 + trade.pnl_equity_pct / 100.0
        busy_until = trade.exit_time
        taken.append(trade)
        curve.append(
            {
                "time": trade.exit_time,
                "equity": round(equity, 6),
                "trade_id": len(taken),
            }
        )
        if max_trades and len(taken) >= max_trades:
            break
        if equity <= 0:
            break
    return pd.DataFrame(curve), taken


def summarize(taken: list[CandidateTrade], curve: pd.DataFrame) -> dict[str, Any]:
    if not taken:
        return {"trades": 0}
    pnl = np.array([trade.pnl_equity_pct for trade in taken])
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    equity = curve.equity.to_numpy()
    peak = np.maximum.accumulate(equity)
    drawdown = (peak - equity) / peak
    return {
        "trades": int(len(taken)),
        "win_rate_pct": round(float((pnl > 0).mean() * 100.0), 4),
        "profit_factor": round(
            float(wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() else (999.0 if len(wins) else 0.0),
            4,
        ),
        "total_pnl_equity_pct": round(float(pnl.sum()), 4),
        "final_equity": round(float(equity[-1]), 6),
        "max_drawdown_pct": round(float(drawdown.max() * 100.0), 4),
        "mean_trade_pct": round(float(pnl.mean()), 4),
        "worst_trade_pct": round(float(pnl.min()), 4),
        "best_trade_pct": round(float(pnl.max()), 4),
        "liquidation_count": int(sum(1 for trade in taken if trade.outcome == "LIQUIDATED")),
        "stop_count": int(sum(1 for trade in taken if trade.outcome == "STOP")),
        "take_profit_count": int(sum(1 for trade in taken if trade.outcome == "TAKE_PROFIT")),
        "time_count": int(sum(1 for trade in taken if trade.outcome == "TIME")),
    }


def _group_metrics(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(
            columns=["group", "trades", "win_rate_pct", "profit_factor", "net_pct"]
        )
    pnl = frame["pnl_equity_pct"].astype(float)
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    return pd.DataFrame(
        {
            "trades": len(pnl),
            "win_rate_pct": round(float((pnl > 0).mean() * 100.0), 4),
            "profit_factor": round(
                float(
                    wins.sum() / abs(losses.sum())
                    if len(losses) and losses.sum()
                    else (999.0 if len(wins) else 0.0)
                ),
                4,
            ),
            "net_pct": round(float(pnl.sum()), 4),
        },
        index=[0],
    )


def main() -> None:
    args = parse_args()
    if not args.data.is_dir():
        raise SystemExit(f"data dir not found: {args.data}")
    if args.symbols:
        symbols = [symbol.upper() for symbol in args.symbols]
    elif args.universe_json is not None:
        if not args.universe_json.exists():
            raise SystemExit(f"universe json not found: {args.universe_json}")
        manifest = json.loads(args.universe_json.read_text(encoding="utf-8"))
        symbols = [str(item["symbol"]).upper() for item in manifest.get("symbols", [])]
    else:
        symbols = sorted(path.stem for path in args.data.glob("*.parquet"))
    all_trades: list[CandidateTrade] = []
    for symbol in symbols:
        path = args.data / f"{symbol}.parquet"
        if not path.exists():
            continue
        frame = load_bars(path)
        if frame is None or frame.empty:
            continue
        frame.attrs["symbol"] = symbol
        start_ms = int(pd.Timestamp(args.start, tz="UTC").timestamp() * 1000)
        end_ms = int(pd.Timestamp(args.end, tz="UTC").timestamp() * 1000)
        frame = frame[(frame.open_time >= start_ms) & (frame.open_time <= end_ms)]
        if frame.empty:
            continue
        candidates = generate_candidates(
            frame,
            args.decision_bar,
            args.lookback_bars,
            args.rule,
            args.min_gap_bars,
            args.allow_short,
            args.vol_z_threshold,
        )
        if not candidates:
            continue
        trades = build_trades(
            frame,
            candidates,
            args.leverage,
            args.risk_pct,
            args.stop_pct,
            args.tp_r,
            args.max_hold_minutes,
            args.fee_bps,
            args.slippage_bps,
            args.maintenance_margin_pct,
        )
        all_trades.extend(trades)
    curve, taken = simulate_equity(all_trades, args.initial_equity, args.max_trades)
    summary = summarize(taken, curve)
    args.output.mkdir(parents=True, exist_ok=True)
    trades_frame = pd.DataFrame([vars(trade) for trade in taken])
    with gzip.open(args.output / "trades.csv.gz", "wt", encoding="utf-8") as handle:
        if taken:
            trades_frame.to_csv(handle, index=False)
    curve.to_csv(args.output / "equity_curve.csv", index=False)
    if not trades_frame.empty:
        year = pd.to_datetime(trades_frame.exit_time, unit="ms", utc=True).dt.year
        by_year = (
            trades_frame.assign(year=year)
            .groupby("year", sort=True)
            .apply(_group_metrics, include_groups=False)
            .reset_index()
        )
        by_symbol = (
            trades_frame.groupby("symbol", sort=True)
            .apply(_group_metrics, include_groups=False)
            .reset_index()
        )
        by_year = by_year.drop(
            columns=[column for column in by_year.columns if column.startswith("level_")]
        )
        by_symbol = by_symbol.drop(
            columns=[column for column in by_symbol.columns if column.startswith("level_")]
        )
        by_year.to_csv(args.output / "by_year.csv", index=False)
        by_symbol.to_csv(args.output / "by_symbol.csv", index=False)
        summary["by_year"] = json.loads(by_year.to_json(orient="records"))
        summary["by_symbol_top"] = json.loads(
            by_symbol.sort_values("net_pct", ascending=False)
            .head(20)
            .to_json(orient="records")
        )
    (args.output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
