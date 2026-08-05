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

from scripts.benchmark_s0_volume_explosion import hourly_bars  # noqa: E402
from scripts.forward_event_monitor import FALLBACK_SYMBOLS, z_score  # noqa: E402
from scripts.research_s0_phase1_highlev_replay import load_bars  # noqa: E402

DEFAULT_DATA = ROOT / "data" / "research" / "binance_um_1m_cross_year" / "parquet"
FUNDING_DIRS = (
    ROOT / "data" / "research" / "binance_um_point_in_time_funding_2020_2023",
    ROOT / "data" / "research" / "binance_um_point_in_time_funding_2024_2025",
    ROOT / "data" / "research" / "binance_um_point_in_time_funding",
)
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_forward_event_rules"
WATCHLIST = tuple(FALLBACK_SYMBOLS)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Historical backtest of the forward-monitor event rules "
            "(funding extreme / volume breakout / BTC impulse) with 24h settlement."
        )
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--funding-threshold-pct", type=float, default=0.05)
    parser.add_argument("--funding-z", type=float, default=2.0)
    parser.add_argument("--vol-z", type=float, default=3.0)
    parser.add_argument("--btc-z", type=float, default=3.0)
    parser.add_argument("--cost-pct", type=float, default=0.24)
    return parser.parse_args()


def funding_events(args: argparse.Namespace) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for directory in FUNDING_DIRS:
        for path in directory.glob("*-funding.parquet"):
            symbol = path.name.split("-funding.parquet")[0].upper()
            frame = pd.read_parquet(path)
            if frame.empty:
                continue
            frame = frame.sort_values("timestamp_ms").reset_index(drop=True)
            rates = (frame.last_funding_rate * 100.0).to_numpy()
            rolling_mean = (
                pd.Series(rates).rolling(499, min_periods=30).mean().shift(1)
            )
            rolling_std = (
                pd.Series(rates).rolling(499, min_periods=30).std().shift(1)
            )
            with np.errstate(invalid="ignore", divide="ignore"):
                z = (rates - rolling_mean) / rolling_std
            events: list[dict[str, Any]] = []
            for index in range(len(rates)):
                latest = float(rates[index])
                z_value = float(z.iloc[index])
                if (
                    abs(latest) >= args.funding_threshold_pct
                    and abs(z_value) >= args.funding_z
                ):
                    events.append(
                        {
                            "symbol": symbol,
                            "ts": int(frame.timestamp_ms.iloc[index]),
                            "direction": -1 if latest > 0 else 1,
                        }
                    )
            if events:
                parts.append(pd.DataFrame(events))
    if not parts:
        return pd.DataFrame(columns=["symbol", "ts", "direction"])
    return pd.concat(parts, ignore_index=True)


def settle_on_hourly(
    events: pd.DataFrame,
    bars: pd.DataFrame,
    hold_hours: int = 24,
) -> pd.DataFrame:
    if events.empty:
        return pd.DataFrame()
    times = bars.open_time.to_numpy()
    opens = bars.open.to_numpy()
    highs = bars.high.to_numpy()
    lows = bars.low.to_numpy()
    closes = bars.close.to_numpy()
    rows: list[dict[str, Any]] = []
    for row in events.itertuples(index=False):
        entry_ms = int(row.ts) + 3_600_000
        start = int(np.searchsorted(times, entry_ms, side="left"))
        if start >= len(times):
            continue
        entry = float(opens[start])
        if entry <= 0:
            continue
        end = min(start + hold_hours, len(times))
        exit_price = float(closes[end - 1])
        mfe = 0.0
        for index in range(start, end):
            favorable = max(
                highs[index] / entry - 1.0,
                entry / lows[index] - 1.0,
            )
            mfe = max(mfe, favorable)
        gross = row.direction * (exit_price / entry - 1.0) * 100.0
        rows.append(
            {
                "symbol": getattr(row, "symbol", ""),
                "ts": row.ts,
                "direction": row.direction,
                "gross_pct": gross,
                "mfe_pct": mfe * 100.0,
            }
        )
    return pd.DataFrame(rows)


def volume_events(bars: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    bars = bars.copy()
    volume = bars.quote_volume
    vol_mean = volume.rolling(199, min_periods=59).mean().shift(1)
    vol_std = volume.rolling(199, min_periods=59).std().shift(1)
    bars["vol_z"] = (volume - vol_mean) / vol_std.replace(0.0, np.nan)
    move = np.sign(bars.close - bars.open)
    bars["direction"] = move
    bars["event"] = (
        bars.vol_z.ge(args.vol_z) & bars.direction.ne(0)
    )
    return bars.loc[bars.event, ["open_time", "direction"]].rename(
        columns={"open_time": "ts"}
    )


def btc_events(bars: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    bars = bars.copy()
    close = bars.close.to_numpy()
    valid = np.arange(4, len(close))
    ret4 = pd.Series(
        close[valid] / close[valid - 4] - 1.0,
        index=bars.index[valid],
        dtype=float,
    )
    bars["ret4"] = ret4
    bars["z"] = bars.ret4.rolling(296, min_periods=56).apply(
        lambda values: z_score(values[:-1].tolist(), float(values[-1])),
        raw=True,
    )
    bars["direction"] = np.sign(bars.ret4)
    bars["event"] = bars.z.abs().ge(args.btc_z) & bars.direction.ne(0)
    return bars.loc[bars.event, ["open_time", "direction"]].rename(
        columns={"open_time": "ts"}
    )


def metrics(trades: pd.DataFrame, cost_pct: float) -> dict[str, Any]:
    if trades.empty:
        return {"trades": 0}
    net = trades.gross_pct - cost_pct
    wins = net.clip(lower=0).sum()
    losses = -net.clip(upper=0).sum()
    return {
        "trades": int(len(trades)),
        "win_rate_pct": round(float((net > 0).mean() * 100.0), 2),
        "profit_factor": round(float(wins / losses), 3) if losses > 0 else 999.0,
        "net_sum_pct": round(float(net.sum()), 3),
        "mean_mfe_pct": round(float(trades.mfe_pct.mean()), 3),
    }


def main() -> None:
    args = parse_args()
    cache: dict[str, pd.DataFrame] = {}

    def bars_for(symbol: str) -> pd.DataFrame | None:
        if symbol not in cache:
            path = args.data / f"{symbol}.parquet"
            if not path.exists():
                return None
            frame = load_bars(path)
            if frame is None or frame.empty:
                return None
            cache[symbol] = hourly_bars(frame)
        return cache[symbol]

    report: dict[str, Any] = {"rules": {}}
    funding = funding_events(args)
    funding_rows: list[pd.DataFrame] = []
    for symbol, scoped in funding.groupby("symbol"):
        bars = bars_for(symbol)
        if bars is None:
            continue
        result = settle_on_hourly(scoped, bars)
        if not result.empty:
            funding_rows.append(result)
    funding_trades = pd.concat(funding_rows) if funding_rows else pd.DataFrame()
    report["rules"]["funding_extreme"] = metrics(funding_trades, args.cost_pct)

    volume_rows: list[pd.DataFrame] = []
    for symbol in WATCHLIST:
        bars = bars_for(symbol)
        if bars is None:
            continue
        events = volume_events(bars, args)
        result = settle_on_hourly(events, bars)
        if not result.empty:
            result["symbol"] = symbol
            volume_rows.append(result)
    volume_trades = pd.concat(volume_rows) if volume_rows else pd.DataFrame()
    report["rules"]["volume_breakout"] = metrics(volume_trades, args.cost_pct)

    btc = bars_for("BTCUSDT")
    btc_trades = (
        settle_on_hourly(btc_events(btc, args), btc) if btc is not None else pd.DataFrame()
    )
    report["rules"]["btc_impulse"] = metrics(btc_trades, args.cost_pct)

    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
