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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.research_s0_phase1_highlev_replay import (  # noqa: E402
    CandidateTrade,
    load_bars,
    simulate_equity,
    simulate_exit,
    summarize,
)

DEFAULT_DATA = ROOT / "data" / "research" / "binance_um_1m_cross_year" / "parquet"
DEFAULT_UNIVERSE = ROOT / "data" / "research" / "s0_public_1m" / "universe.json"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_btc_impulse_alt"
MINUTE_MS = 60_000


@dataclass(frozen=True)
class BtcEvent:
    time_ms: int
    z: float
    direction: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Event backtest: extreme BTC moves (impulse) -> enter the most liquid "
            "listed alt in the same direction at the next 1m open, exit fast with "
            "stop/TP/time. Point-in-time volume ranking, one position at a time, "
            "compounding equity."
        )
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--universe-json", type=Path, default=DEFAULT_UNIVERSE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default="2026-08-06")
    parser.add_argument("--event-bar", type=int, default=5, help="BTC event bar minutes")
    parser.add_argument("--lookback", type=int, default=30, help="BTC return lookback minutes")
    parser.add_argument("--z-threshold", type=float, default=3.0)
    parser.add_argument("--std-window-bars", type=int, default=1200)
    parser.add_argument("--entry-delay-min", type=int, default=0)
    parser.add_argument("--cooldown-min", type=int, default=120)
    parser.add_argument(
        "--trend-filter-hours",
        type=int,
        default=0,
        help="If >0, only trade events whose BTC trailing return over N hours has the same sign.",
    )
    parser.add_argument("--min-21d-volume-usdt", type=float, default=20_000_000.0)
    parser.add_argument(
        "--max-alts",
        type=int,
        default=15,
        help="Only cache the top-N alts by current quote volume to bound memory.",
    )
    parser.add_argument("--leverage", type=float, default=5.0)
    parser.add_argument("--risk-pct", type=float, default=30.0)
    parser.add_argument("--stop-pct", type=float, default=1.0)
    parser.add_argument("--tp-r", type=float, default=2.0)
    parser.add_argument("--max-hold-minutes", type=int, default=60)
    parser.add_argument("--fee-bps", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    parser.add_argument("--maintenance-margin-pct", type=float, default=0.5)
    parser.add_argument("--initial-equity", type=float, default=14.0)
    parser.add_argument("--allow-short", action="store_true")
    parser.add_argument("--max-trades", type=int, default=0)
    return parser.parse_args()


def detect_events(
    btc: pd.DataFrame,
    event_bar_minutes: int,
    lookback_minutes: int,
    z_threshold: float,
    std_window_bars: int,
    allow_short: bool,
) -> list[BtcEvent]:
    bars = btc.copy()
    bars["bar"] = bars.open_time // (event_bar_minutes * MINUTE_MS)
    agg = (
        bars.groupby("bar", as_index=False)
        .agg(close=("close", "last"), open_time=("open_time", "first"))
        .sort_values("bar")
        .reset_index(drop=True)
    )
    step = max(1, lookback_minutes // event_bar_minutes)
    agg["ret"] = agg.close.pct_change(step)
    # Volatility uses only returns that closed before the current bar.
    agg["vol"] = (
        agg.ret.shift(1)
        .rolling(std_window_bars, min_periods=max(60, std_window_bars // 4))
        .std()
    )
    agg["z"] = agg.ret / agg.vol.replace(0.0, np.nan)
    events: list[BtcEvent] = []
    for row in agg.itertuples(index=False):
        if not np.isfinite(row.z):
            continue
        if row.z >= z_threshold:
            events.append(
                BtcEvent(
                    time_ms=int(row.open_time) + event_bar_minutes * MINUTE_MS,
                    z=float(row.z),
                    direction=1,
                )
            )
        elif allow_short and row.z <= -z_threshold:
            events.append(
                BtcEvent(
                    time_ms=int(row.open_time) + event_bar_minutes * MINUTE_MS,
                    z=float(row.z),
                    direction=-1,
                )
            )
    return events


def rolling_daily_volume(frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    day = pd.to_datetime(frame.open_time, unit="ms", utc=True).dt.normalize()
    daily = frame.assign(day=day).groupby("day", sort=True)["quote_volume"].sum()
    rolling = daily.rolling(21, min_periods=7).mean()
    return np.asarray(daily.index.view("int64")), rolling.to_numpy()


def build_alt_cache(
    data: Path,
    universe: list[dict[str, Any]],
    max_alts: int,
) -> dict[str, tuple[pd.DataFrame, np.ndarray, np.ndarray, int]]:
    cache: dict[str, tuple[pd.DataFrame, np.ndarray, np.ndarray, int]] = {}
    for item in universe[:max_alts]:
        symbol = str(item["symbol"])
        if symbol == "BTCUSDT":
            continue
        path = data / f"{symbol}.parquet"
        if not path.exists():
            continue
        frame = load_bars(path)
        if frame is None or frame.empty:
            continue
        day_idx, rv = rolling_daily_volume(frame)
        cache[symbol] = (frame, day_idx, rv, int(item.get("onboard_date", 0)))
    return cache


def select_alt(
    cache: dict[str, tuple[pd.DataFrame, np.ndarray, np.ndarray, int]],
    event_time_ms: int,
    min_volume: float,
) -> str | None:
    best: str | None = None
    best_volume = -1.0
    for symbol, (frame, day_idx, rv, onboard) in cache.items():
        if onboard > event_time_ms:
            continue
        position = int(np.searchsorted(day_idx, event_time_ms, side="right")) - 1
        if position < 0 or not np.isfinite(rv[position]):
            continue
        volume = float(rv[position])
        if volume >= min_volume and volume > best_volume:
            best = symbol
            best_volume = volume
    return best


def build_trades(
    events: list[BtcEvent],
    cache: dict[str, tuple[pd.DataFrame, np.ndarray, np.ndarray, int]],
    entry_delay_min: int,
    min_volume: float,
    leverage: float,
    risk_pct: float,
    stop_pct: float,
    tp_r: float,
    max_hold_minutes: int,
    fee_bps: float,
    slippage_bps: float,
    maintenance_margin_pct: float,
    cooldown_min: int,
    btc: pd.DataFrame | None = None,
    trend_filter_hours: int = 0,
) -> list[CandidateTrade]:
    effective_stop = min(stop_pct, risk_pct / leverage)
    liq_pct = 100.0 / leverage - maintenance_margin_pct
    cost_pct_equity = (fee_bps + slippage_bps) * 2.0 * leverage / 100.0
    trades: list[CandidateTrade] = []
    last_entry = -10**18
    btc_times = btc.open_time.to_numpy() if btc is not None else None
    btc_closes = btc.close.to_numpy() if btc is not None else None
    for event in events:
        entry_floor = event.time_ms + entry_delay_min * MINUTE_MS
        if entry_floor < last_entry + cooldown_min * MINUTE_MS:
            continue
        if trend_filter_hours > 0 and btc_times is not None:
            now_position = int(
                np.searchsorted(btc_times, event.time_ms, side="left")
            ) - 1
            lag_time = event.time_ms - trend_filter_hours * 3_600_000
            lag_position = int(
                np.searchsorted(btc_times, lag_time, side="left")
            ) - 1
            if now_position < 0 or lag_position < 0:
                continue
            trend_ret = float(
                btc_closes[now_position] / btc_closes[lag_position] - 1.0
            )
            if not np.isfinite(trend_ret) or trend_ret * event.direction <= 0:
                continue
        symbol = select_alt(cache, event.time_ms, min_volume)
        if symbol is None:
            continue
        frame, _, _, _ = cache[symbol]
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
                if event.direction == 1
                else entry_price * (1.0 + liq_pct / 100.0)
            )
            pnl_pct = -100.0
            hold = 1
        else:
            result = simulate_exit(
                frame,
                entry_ms,
                entry_price,
                event.direction,
                effective_stop,
                tp_r,
                max_hold_minutes,
            )
            if result is None:
                continue
            exit_ms, exit_price, outcome, hold = result
            gross = (
                (exit_price / entry_price - 1.0) * 100.0 * leverage * event.direction
            )
            pnl_pct = gross - cost_pct_equity
        trades.append(
            CandidateTrade(
                symbol=symbol,
                entry_time=int(entry_ms),
                exit_time=int(exit_ms),
                direction=event.direction,
                entry_price=entry_price,
                exit_price=exit_price,
                outcome=outcome,
                pnl_equity_pct=float(pnl_pct),
                hold_minutes=int(hold),
            )
        )
        last_entry = entry_ms
    return trades


def main() -> None:
    args = parse_args()
    if not args.data.is_dir():
        raise SystemExit(f"data dir not found: {args.data}")
    universe = json.loads(args.universe_json.read_text(encoding="utf-8"))
    items = universe.get("symbols", [])
    btc_path = args.data / "BTCUSDT.parquet"
    if not btc_path.exists():
        raise SystemExit("BTCUSDT.parquet not found in data dir")
    btc = load_bars(btc_path)
    if btc is None:
        raise SystemExit("failed to load BTCUSDT")
    start_ms = int(pd.Timestamp(args.start, tz="UTC").timestamp() * 1000)
    end_ms = int(pd.Timestamp(args.end, tz="UTC").timestamp() * 1000)
    btc = btc[(btc.open_time >= start_ms) & (btc.open_time <= end_ms)]
    events = detect_events(
        btc,
        args.event_bar,
        args.lookback,
        args.z_threshold,
        args.std_window_bars,
        args.allow_short,
    )
    cache = build_alt_cache(args.data, items, args.max_alts)
    trades = build_trades(
        events,
        cache,
        args.entry_delay_min,
        args.min_21d_volume_usdt,
        args.leverage,
        args.risk_pct,
        args.stop_pct,
        args.tp_r,
        args.max_hold_minutes,
        args.fee_bps,
        args.slippage_bps,
        args.maintenance_margin_pct,
        args.cooldown_min,
        btc,
        args.trend_filter_hours,
    )
    curve, taken = simulate_equity(trades, args.initial_equity, args.max_trades)
    summary = summarize(taken, curve)
    summary["events"] = len(events)
    summary["events_with_alt"] = len(trades)
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
