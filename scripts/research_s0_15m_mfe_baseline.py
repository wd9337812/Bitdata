from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.research_s0_phase1_highlev_replay import load_bars  # noqa: E402

DEFAULT_DATA = ROOT / "data" / "research" / "binance_um_1m_cross_year" / "parquet"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_15m_mfe_baseline"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT", "ADAUSDT", "BNBUSDT", "LINKUSDT")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Bounded 15m tail-MFE prototype: volume z + taker imbalance ignition "
            "rule, stop 1%, TP 3R, 2h hold, single position."
        )
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--symbols", nargs="+", default=list(SYMBOLS))
    parser.add_argument("--vol-z", type=float, default=2.0)
    parser.add_argument("--taker-imbalance", type=float, default=0.15)
    parser.add_argument("--min-move-pct", type=float, default=1.0)
    parser.add_argument("--stop-pct", type=float, default=1.0)
    parser.add_argument("--tp-r", type=float, default=3.0)
    parser.add_argument("--hold-bars", type=int, default=8)
    parser.add_argument("--cost-pct", type=float, default=0.24)
    return parser.parse_args()


def fifteen_minute(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    frame["bar"] = frame.open_time // 900_000
    agg = (
        frame.groupby("bar", as_index=False)
        .agg(
            open_time=("open_time", "first"),
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            quote_volume=("quote_volume", "sum"),
            taker_buy_quote=("taker_buy_quote_volume", "sum"),
        )
        .sort_values("bar")
        .reset_index(drop=True)
    )
    prev_close = agg.close.shift(1)
    true_range = pd.concat(
        [agg.high - agg.low, (agg.high - prev_close).abs(), (agg.low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    agg["atr"] = true_range.rolling(16, min_periods=8).mean().shift(1)
    vol_mean = agg.quote_volume.rolling(96, min_periods=48).mean().shift(1)
    vol_std = agg.quote_volume.rolling(96, min_periods=48).std().shift(1)
    agg["vol_z"] = (agg.quote_volume - vol_mean) / vol_std.replace(0.0, np.nan)
    agg["taker_sell"] = (agg.quote_volume - agg.taker_buy_quote).clip(lower=0.0)
    agg["taker_imbalance"] = (
        (agg.taker_buy_quote - agg.taker_sell)
        / (agg.taker_buy_quote + agg.taker_sell).replace(0.0, np.nan)
    )
    agg["ret_1"] = agg.close.pct_change()
    return agg


def signals_for(bars: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    bars = bars.copy()
    move = np.sign(bars.ret_1)
    taker_dir = np.sign(bars.taker_imbalance)
    bars["direction"] = move
    bars["signal"] = (
        bars.vol_z.ge(args.vol_z)
        & bars.taker_imbalance.abs().ge(args.taker_imbalance)
        & bars.ret_1.abs().ge(args.min_move_pct / 100.0)
        & bars.direction.eq(taker_dir)
        & bars.direction.ne(0)
        & bars.atr.gt(0)
    )
    return bars.loc[bars.signal, [
        "open_time", "open", "high", "low", "close", "atr", "direction", "vol_z", "taker_imbalance"
    ]].copy()


def simulate(
    signals: pd.DataFrame,
    bars: pd.DataFrame,
    args: argparse.Namespace,
) -> pd.DataFrame:
    ordered = signals.sort_values("open_time").reset_index(drop=True)
    trades: list[dict[str, Any]] = []
    busy_until = -1
    for row in ordered.itertuples(index=False):
        entry_ms = int(row.open_time) + 900_000
        if entry_ms < busy_until:
            continue
        start = int(bars.open_time.searchsorted(entry_ms, side="left"))
        path = bars.iloc[start : start + args.hold_bars]
        if path.empty or int(path.iloc[0].open_time) != entry_ms:
            continue
        entry = float(path.iloc[0].open)
        stop = entry - row.direction * entry * args.stop_pct / 100.0
        target = entry + row.direction * entry * args.stop_pct * args.tp_r / 100.0
        exit_price = float(path.iloc[-1].close)
        outcome = "TIME"
        exit_ms = int(path.iloc[-1].open_time) + 900_000
        for bar in path.itertuples(index=False):
            if row.direction > 0 and float(bar.low) <= stop:
                exit_price, outcome = stop, "STOP"
            elif row.direction < 0 and float(bar.high) >= stop:
                exit_price, outcome = stop, "STOP"
            elif row.direction > 0 and float(bar.high) >= target:
                exit_price, outcome = target, "TARGET"
            elif row.direction < 0 and float(bar.low) <= target:
                exit_price, outcome = target, "TARGET"
            else:
                continue
            exit_ms = int(bar.open_time) + 900_000
            break
        gross_pct = row.direction * (exit_price / entry - 1.0) * 100.0
        trades.append(
            {
                "symbol": row.symbol,
                "entry_ms": entry_ms,
                "exit_ms": exit_ms,
                "direction": int(row.direction),
                "gross_pct": gross_pct,
                "outcome": outcome,
            }
        )
        busy_until = exit_ms
    return pd.DataFrame(trades)


def main() -> None:
    args = parse_args()
    all_signals: list[pd.DataFrame] = []
    bars_cache: dict[str, pd.DataFrame] = {}
    for symbol in args.symbols:
        path = args.data / f"{symbol}.parquet"
        if not path.exists():
            continue
        frame = load_bars(path)
        if frame is None or frame.empty:
            continue
        bars = fifteen_minute(frame)
        if len(bars) < 150:
            continue
        bars_cache[symbol] = bars
        sig = signals_for(bars, args)
        if not sig.empty:
            sig["symbol"] = symbol
            all_signals.append(sig)
    if not all_signals:
        raise SystemExit("no signals")
    combined = pd.concat(all_signals, ignore_index=True)
    trades: list[pd.DataFrame] = []
    for symbol, sig in combined.groupby("symbol"):
        result = simulate(sig, bars_cache[symbol], args)
        if not result.empty:
            trades.append(result)
    trades = pd.concat(trades, ignore_index=True) if trades else pd.DataFrame()
    args.output.mkdir(parents=True, exist_ok=True)
    trades.to_parquet(args.output / "trades.parquet", index=False)
    report: dict[str, Any] = {
        "experiment": "s0_15m_mfe_baseline",
        "symbols": list(bars_cache.keys()),
        "signals": int(len(combined)),
        "trades": int(len(trades)),
        "annual": {},
    }
    if not trades.empty:
        net = trades.gross_pct - args.cost_pct
        year = pd.to_datetime(trades.entry_ms, unit="ms", utc=True).dt.year
        for value in sorted(trades.assign(year=year).year.unique()):
            scoped = trades.loc[year == value]
            sub_net = scoped.gross_pct - args.cost_pct
            wins = sub_net.clip(lower=0).sum()
            losses = -sub_net.clip(upper=0).sum()
            report["annual"][str(value)] = {
                "trades": int(len(scoped)),
                "win_rate_pct": round(float((sub_net > 0).mean() * 100.0), 2),
                "profit_factor": round(float(wins / losses), 3) if losses > 0 else 999.0,
                "net_sum_pct": round(float(sub_net.sum()), 3),
            }
        report["overall"] = {
            "trades": int(len(trades)),
            "win_rate_pct": round(float((net > 0).mean() * 100.0), 2),
            "profit_factor": round(
                float(net.clip(lower=0).sum() / -net.clip(upper=0).sum()), 3
            )
            if (net < 0).any()
            else 999.0,
            "net_sum_pct": round(float(net.sum()), 3),
        }
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
