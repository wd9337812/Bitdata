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
DEFAULT_UNIVERSE = ROOT / "data" / "research" / "s0_public_1m" / "universe.json"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_volume_explosion"

WINDOWS = {
    "development_2020_2023": ("2020-01-01", "2024-01-01"),
    "validation_2024": ("2024-01-01", "2025-01-01"),
    "test_2025": ("2025-01-01", "2026-01-01"),
    "final_blind_2026": ("2026-01-01", "2027-01-01"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Pre-registered 1h volume-explosion continuation: quote volume >=5x "
            "24h mean, |taker imbalance| >=0.15, 30d momentum same direction; "
            "2.5 ATR stop, 10R target, 5-day cap, single position."
        )
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--universe-json", type=Path, default=DEFAULT_UNIVERSE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--volume-ratio", type=float, default=5.0)
    parser.add_argument("--taker-imbalance", type=float, default=0.15)
    parser.add_argument("--min-24h-volume-usdt", type=float, default=20_000_000.0)
    parser.add_argument("--stop-atr", type=float, default=2.5)
    parser.add_argument("--target-r", type=float, default=10.0)
    parser.add_argument("--max-hold-hours", type=int, default=120)
    parser.add_argument("--embargo-hours", type=int, default=72)
    parser.add_argument("--base-cost-pct", type=float, default=0.12)
    parser.add_argument("--stress-cost-pct", type=float, default=0.24)
    return parser.parse_args()


def hourly_bars(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    frame["hour"] = frame.open_time // 3_600_000
    agg = (
        frame.groupby("hour", as_index=False)
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            quote_volume=("quote_volume", "sum"),
            taker_buy_quote=("taker_buy_quote_volume", "sum"),
            open_time=("open_time", "first"),
        )
        .sort_values("hour")
        .reset_index(drop=True)
    )
    if agg.empty:
        return agg
    prev_close = agg.close.shift(1)
    true_range = pd.concat(
        [
            agg.high - agg.low,
            (agg.high - prev_close).abs(),
            (agg.low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    agg["atr"] = true_range.rolling(14, min_periods=7).mean().shift(1)
    agg["vol_mean_24h"] = (
        agg.quote_volume.rolling(24, min_periods=12).mean().shift(1)
    )
    agg["taker_sell_quote"] = (
        agg.quote_volume - agg.taker_buy_quote
    ).clip(lower=0.0)
    agg["taker_imbalance"] = (
        (agg.taker_buy_quote - agg.taker_sell_quote)
        / (agg.taker_buy_quote + agg.taker_sell_quote).replace(0.0, np.nan)
    )
    agg["ret_720h"] = agg.close.pct_change(720)
    return agg


def signals_for(hourly: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    hourly = hourly.copy()
    hourly["vol_ratio"] = hourly.quote_volume / hourly.vol_mean_24h.replace(0.0, np.nan)
    move = np.sign(hourly.close - hourly.open)
    taker_dir = np.sign(hourly.taker_imbalance)
    momentum_dir = np.sign(hourly.ret_720h)
    hourly["direction"] = move
    hourly["signal"] = (
        hourly.vol_ratio.ge(args.volume_ratio)
        & hourly.taker_imbalance.abs().ge(args.taker_imbalance)
        & hourly.vol_mean_24h.ge(args.min_24h_volume_usdt)
        & hourly.atr.gt(0)
        & hourly.direction.eq(taker_dir)
        & hourly.direction.eq(momentum_dir)
        & hourly.direction.ne(0)
    )
    return hourly.loc[hourly.signal, [
        "open_time", "open", "high", "low", "close", "atr", "direction", "vol_ratio", "taker_imbalance"
    ]].copy()


def simulate(
    signals: pd.DataFrame,
    hourly: pd.DataFrame,
    args: argparse.Namespace,
) -> pd.DataFrame:
    ordered = signals.sort_values("open_time").reset_index(drop=True)
    trades: list[dict[str, Any]] = []
    busy_until = -1
    last_by_symbol: dict[str, int] = {}
    for row in ordered.itertuples(index=False):
        entry_ms = int(row.open_time) + 3_600_000
        if entry_ms < busy_until:
            continue
        if entry_ms - last_by_symbol.get(row.symbol, -10**18) < args.embargo_hours * 3_600_000:
            continue
        start = int(hourly.open_time.searchsorted(entry_ms, side="left"))
        end = min(start + args.max_hold_hours, len(hourly))
        path = hourly.iloc[start:end]
        if path.empty or int(path.iloc[0].open_time) != entry_ms:
            continue
        entry = float(path.iloc[0].open)
        stop_distance = args.stop_atr * float(row.atr)
        stop = entry - row.direction * stop_distance
        target = entry + row.direction * stop_distance * args.target_r
        exit_price = float(path.iloc[-1].close)
        outcome = "TIME"
        exit_ms = int(path.iloc[-1].open_time) + 3_600_000
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
            exit_ms = int(bar.open_time) + 3_600_000
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
                "vol_ratio": float(row.vol_ratio),
                "taker_imbalance": float(row.taker_imbalance),
            }
        )
        busy_until = exit_ms
        last_by_symbol[row.symbol] = entry_ms
    return pd.DataFrame(trades)


def metrics(trades: pd.DataFrame, cost_pct: float) -> dict[str, Any]:
    if trades.empty:
        return {"trades": 0, "profit_factor": 0.0, "net_pct_points": 0.0}
    net = trades.gross_pct - cost_pct
    wins = net.clip(lower=0).sum()
    losses = -net.clip(upper=0).sum()
    return {
        "trades": int(len(trades)),
        "symbols": int(trades.symbol.nunique()),
        "win_rate_pct": round(float((net > 0).mean() * 100.0), 2),
        "profit_factor": round(float(wins / losses), 4) if losses > 0 else 999.0,
        "net_pct_points": round(float(net.sum()), 4),
        "mean_net_pct": round(float(net.mean()), 4),
        "max_drawdown_pct_points": round(float((net.cumsum().cummax() - net.cumsum()).max()), 4),
    }


def main() -> None:
    args = parse_args()
    universe = json.loads(args.universe_json.read_text(encoding="utf-8"))["symbols"]
    symbols = [str(item["symbol"]) for item in universe]
    all_signals: list[pd.DataFrame] = []
    hourly_cache: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        path = args.data / f"{symbol}.parquet"
        if not path.exists():
            continue
        frame = load_bars(path)
        if frame is None or frame.empty or "taker_buy_quote_volume" not in frame.columns:
            continue
        hourly = hourly_bars(frame)
        if hourly.empty or len(hourly) < 750:
            continue
        hourly_cache[symbol] = hourly
        sig = signals_for(hourly, args)
        if not sig.empty:
            sig["symbol"] = symbol
            all_signals.append(sig)
    if not all_signals:
        raise SystemExit("no signals")
    combined_signals = pd.concat(all_signals, ignore_index=True)
    all_trades: list[pd.DataFrame] = []
    for symbol, sig in combined_signals.groupby("symbol"):
        trades = simulate(sig, hourly_cache[symbol], args)
        if not trades.empty:
            all_trades.append(trades)
    trades = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    args.output.mkdir(parents=True, exist_ok=True)
    trades.to_parquet(args.output / "trades.parquet", index=False)
    report: dict[str, Any] = {
        "experiment": "s0_volume_explosion",
        "signals": int(len(combined_signals)),
        "trades": int(len(trades)),
        "windows": {},
        "annual": {},
    }
    for name, (start, end) in WINDOWS.items():
        scoped = trades[
            pd.to_datetime(trades.entry_ms, unit="ms", utc=True).between(
                pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
            )
        ]
        report["windows"][name] = {
            "base": metrics(scoped, args.base_cost_pct),
            "stress": metrics(scoped, args.stress_cost_pct),
        }
    if not trades.empty:
        year = pd.to_datetime(trades.entry_ms, unit="ms", utc=True).dt.year
        for value in sorted(trades.assign(year=year).year.unique()):
            report["annual"][str(value)] = metrics(
                trades.loc[year == value], args.stress_cost_pct
            )
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
