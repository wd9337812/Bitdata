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

from scripts.research_s0_intraday_xmom import (  # noqa: E402
    MINUTE_MS,
    CandidateTrade,
    simulate_equity,
    simulate_exits_vectorized,
)
from scripts.research_s0_phase1_highlev_replay import load_bars  # noqa: E402

DEFAULT_DATA = ROOT / "data" / "research" / "binance_um_1m_cross_year" / "parquet"
DEFAULT_UNIVERSE = ROOT / "data" / "research" / "s0_public_1m" / "universe.json"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_funding_fast_fade"
FUNDING_DIRS = (
    ROOT / "data" / "research" / "binance_um_point_in_time_funding_2020_2023",
    ROOT / "data" / "research" / "binance_um_point_in_time_funding_2024_2025",
    ROOT / "data" / "research" / "binance_um_point_in_time_funding",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Funding-crowding fast fade: after an extreme funding settlement, fade "
            "the crowded side on the most crowded liquid symbol, entering at the "
            "next 1m open with fast stop/TP/time exits."
        )
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--universe-json", type=Path, default=DEFAULT_UNIVERSE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default="2026-08-06")
    parser.add_argument("--min-funding-pct", type=float, default=0.10)
    parser.add_argument("--entry-delay-min", type=int, default=0)
    parser.add_argument("--min-21d-volume-usdt", type=float, default=20_000_000.0)
    parser.add_argument("--max-alts", type=int, default=30)
    parser.add_argument("--leverage", type=float, default=5.0)
    parser.add_argument("--risk-pct", type=float, default=30.0)
    parser.add_argument("--stop-pct", type=float, default=1.0)
    parser.add_argument("--tp-r", type=float, default=2.0)
    parser.add_argument("--max-hold-minutes", type=int, default=60)
    parser.add_argument("--fee-bps", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    parser.add_argument("--initial-equity", type=float, default=14.0)
    parser.add_argument("--max-trades", type=int, default=0)
    return parser.parse_args()


def load_funding() -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for directory in FUNDING_DIRS:
        for path in directory.glob("*-funding.parquet"):
            frame = pd.read_parquet(
                path,
                columns=["timestamp_ms", "symbol", "last_funding_rate"],
            )
            frame["symbol"] = frame.symbol.str.upper()
            parts.append(frame)
    combined = (
        pd.concat(parts, ignore_index=True)
        .drop_duplicates(["symbol", "timestamp_ms"], keep="last")
        .sort_values(["timestamp_ms", "symbol"])
        .reset_index(drop=True)
    )
    return combined


def build_events(
    funding: pd.DataFrame,
    panel: dict[str, dict[str, np.ndarray]],
    start_ms: int,
    end_ms: int,
    min_funding_pct: float,
    min_volume: float,
) -> list[tuple[int, str, int, float]]:
    threshold = min_funding_pct / 100.0
    events: list[tuple[int, str, int, float]] = []
    grouped = funding[
        (funding.timestamp_ms >= start_ms) & (funding.timestamp_ms <= end_ms)
    ].groupby("timestamp_ms", sort=True)
    for timestamp, scoped in grouped:
        best_symbol: str | None = None
        best_direction = 0
        best_abs = -1.0
        for row in scoped.itertuples(index=False):
            symbol = str(row.symbol)
            arrays = panel.get(symbol)
            if arrays is None:
                continue
            rate = float(row.last_funding_rate)
            if not np.isfinite(rate) or abs(rate) < threshold:
                continue
            position = int(
                np.searchsorted(
                    arrays["open_time"], int(timestamp), side="left"
                )
            ) - 1
            if position < 0:
                continue
            if float(arrays["vol21"][position]) < min_volume:
                continue
            if int(arrays["onboard"]) > int(timestamp):
                continue
            if abs(rate) > best_abs:
                best_symbol = symbol
                best_abs = abs(rate)
                best_direction = -1 if rate > 0 else 1
        if best_symbol is not None:
            events.append(
                (int(timestamp), best_symbol, best_direction, best_abs)
            )
    return events


def build_trades(
    events: list[tuple[int, str, int, float]],
    frame_cache: dict[str, pd.DataFrame],
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
    grouped: dict[str, tuple[list[int], list[float], list[int]]] = {}
    for timestamp, symbol, direction, _rate in events:
        entry_floor = timestamp + delay * MINUTE_MS
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
    return trades


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
        "take_profit_count": int(
            sum(1 for trade in taken if trade.outcome == "TAKE_PROFIT")
        ),
        "time_count": int(sum(1 for trade in taken if trade.outcome == "TIME")),
    }


def main() -> None:
    args = parse_args()
    items = json.loads(args.universe_json.read_text(encoding="utf-8"))["symbols"]
    start_ms = int(pd.Timestamp(args.start, tz="UTC").timestamp() * 1000)
    end_ms = int(pd.Timestamp(args.end, tz="UTC").timestamp() * 1000)
    funding = load_funding()
    frame_cache: dict[str, pd.DataFrame] = {}
    panel: dict[str, dict[str, np.ndarray]] = {}
    for item in items[: args.max_alts]:
        symbol = str(item["symbol"])
        if symbol == "BTCUSDT":
            continue
        path = args.data / f"{symbol}.parquet"
        if not path.exists():
            continue
        frame = load_bars(path)
        if frame is None or frame.empty:
            continue
        frame_cache[symbol] = frame
        from scripts.research_s0_btc_impulse_alt import rolling_daily_volume

        day_idx, rv = rolling_daily_volume(frame)
        minute_times = frame.open_time.to_numpy(dtype="int64")
        minute_days = pd.Series(
            pd.to_datetime(minute_times, unit="ms", utc=True)
        ).dt.normalize()
        day_positions = np.clip(
            np.searchsorted(
                day_idx,
                minute_days.astype("int64").to_numpy(),
                side="right",
            )
            - 1,
            0,
            len(day_idx) - 1,
        )
        panel[symbol] = {
            "open_time": minute_times,
            "vol21": np.where(np.isfinite(rv[day_positions]), rv[day_positions], np.nan),
            "onboard": np.int64(item.get("onboard_date", 0)),
        }
    events = build_events(
        funding,
        panel,
        start_ms,
        end_ms,
        args.min_funding_pct,
        args.min_21d_volume_usdt,
    )
    trades = build_trades(
        events,
        frame_cache,
        args.data,
        args.entry_delay_min,
        args.leverage,
        args.risk_pct,
        args.stop_pct,
        args.tp_r,
        args.max_hold_minutes,
        args.fee_bps,
        args.slippage_bps,
    )
    curve, taken = simulate_equity(trades, args.initial_equity, args.max_trades)
    summary = summarize(taken)
    summary["events"] = len(events)
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
