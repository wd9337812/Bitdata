from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MINUTE_DIR = ROOT / "data" / "research" / "s0_public_1m" / "parquet"
DEFAULT_OUTPUT = (
    ROOT / "data" / "research" / "s0_cross_sectional_momentum_v1"
)
COST_PCT = 0.12


@dataclass(frozen=True)
class Profile:
    name: str
    score_columns: tuple[str, ...]
    quantile: float
    stop_atr: float
    reward_r: float
    hold_hours: int
    max_stop_pct: float = 15.0


PROFILES = (
    Profile("momentum_blend_5pct", ("ret_6h", "ret_24h", "ret_72h"), 0.05, 1.2, 1.8, 12),
    Profile("momentum_24h_5pct", ("ret_24h",), 0.05, 1.2, 1.8, 12),
    Profile("momentum_72h_5pct", ("ret_72h",), 0.05, 1.4, 2.0, 24),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark a no-lookahead cross-sectional altcoin momentum baseline."
    )
    parser.add_argument("--minute-dir", type=Path, default=DEFAULT_MINUTE_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-symbols", type=int, default=0)
    return parser.parse_args()


def hourly_bars(path: Path) -> pd.DataFrame:
    frame = pd.read_parquet(
        path,
        columns=["symbol", "open_time", "open", "high", "low", "close", "quote_volume"],
    ).sort_values("open_time")
    timestamp = pd.to_datetime(frame.open_time, unit="ms", utc=True)
    frame["hour"] = timestamp.dt.floor("h")
    grouped = frame.groupby("hour", sort=True)
    result = grouped.agg(
        symbol=("symbol", "last"),
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        quote_volume=("quote_volume", "sum"),
        minute_count=("open_time", "size"),
    )
    result = result[result.minute_count.ge(55)].copy()
    previous_close = result.close.shift(1)
    true_range = pd.concat(
        [
            result.high - result.low,
            (result.high - previous_close).abs(),
            (result.low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    result["atr_24h"] = true_range.rolling(24, min_periods=20).mean()
    for hours in (6, 24, 72):
        result[f"ret_{hours}h"] = result.close.pct_change(hours)
    result["liquidity_24h"] = result.quote_volume.rolling(24, min_periods=20).sum()
    result["available_ms"] = (
        result.index.tz_convert("UTC").tz_localize(None).to_numpy(dtype="datetime64[ms]")
        .astype("int64")
        + 3_600_000
    )
    return result.reset_index(drop=True)


def build_panel(minute_dir: Path, max_symbols: int = 0) -> pd.DataFrame:
    paths = sorted(minute_dir.glob("*.parquet"))
    if max_symbols > 0:
        paths = paths[:max_symbols]
    parts = [hourly_bars(path) for path in paths]
    panel = pd.concat(parts, ignore_index=True)
    panel = panel.dropna(
        subset=["ret_6h", "ret_24h", "ret_72h", "atr_24h", "liquidity_24h"]
    ).copy()
    return panel.sort_values(["available_ms", "symbol"]).reset_index(drop=True)


def rank_signals(panel: pd.DataFrame, profile: Profile) -> pd.DataFrame:
    result = panel.copy()
    for column in profile.score_columns:
        result[f"{column}_rank"] = result.groupby("available_ms", sort=False)[
            column
        ].rank(pct=True)
    result["momentum_score"] = result[
        [f"{column}_rank" for column in profile.score_columns]
    ].mean(axis=1)
    result["liquidity_rank"] = result.groupby("available_ms", sort=False)[
        "liquidity_24h"
    ].rank(pct=True)
    breadth = result.groupby("available_ms", sort=False).ret_24h.median()
    btc = (
        result[result.symbol.eq("BTCUSDT")]
        .set_index("available_ms")
        .ret_24h
    )
    result["market_breadth_24h"] = result.available_ms.map(breadth)
    result["btc_ret_24h"] = result.available_ms.map(btc)
    result["market_direction"] = np.select(
        [
            result.market_breadth_24h.gt(0) & result.btc_ret_24h.gt(0),
            result.market_breadth_24h.lt(0) & result.btc_ret_24h.lt(0),
        ],
        ["LONG", "SHORT"],
        default="MIXED",
    )
    liquid = result.liquidity_rank.ge(0.5)
    long_signal = (
        result.market_direction.eq("LONG")
        & result.momentum_score.ge(1.0 - profile.quantile)
    )
    short_signal = (
        result.market_direction.eq("SHORT")
        & result.momentum_score.le(profile.quantile)
    )
    candidates = result[liquid & (long_signal | short_signal)].copy()
    candidates["direction"] = candidates.market_direction
    candidates["strength"] = np.where(
        candidates.direction.eq("LONG"),
        candidates.momentum_score,
        1.0 - candidates.momentum_score,
    )
    return (
        candidates.sort_values(
            ["available_ms", "strength", "liquidity_rank"],
            ascending=[True, False, False],
        )
        .groupby("available_ms", sort=False)
        .head(1)
        .reset_index(drop=True)
    )


def simulate(
    signals: pd.DataFrame,
    panel: pd.DataFrame,
    profile: Profile,
    cost_pct: float = COST_PCT,
    execution_delay_hours: int = 0,
) -> pd.DataFrame:
    bars = {
        symbol: scoped.sort_values("available_ms").set_index("available_ms")
        for symbol, scoped in panel.groupby("symbol", sort=False)
    }
    trades: list[dict[str, Any]] = []
    next_available_ms = -1
    for signal in signals.itertuples(index=False):
        if signal.available_ms < next_available_ms:
            continue
        scoped = bars.get(signal.symbol)
        entry_bar_available_ms = int(signal.available_ms) + (
            execution_delay_hours + 1
        ) * 3_600_000
        if scoped is None or entry_bar_available_ms not in scoped.index:
            continue
        # The signal is known only when the current hour closes. The bar whose
        # available_ms is one hour later opens exactly at the decision time.
        start_position = int(scoped.index.searchsorted(entry_bar_available_ms))
        path = scoped.iloc[start_position : start_position + profile.hold_hours]
        if path.empty:
            continue
        entry = float(path.iloc[0].open)
        atr = float(signal.atr_24h)
        if not np.isfinite(entry) or not np.isfinite(atr) or entry <= 0 or atr <= 0:
            continue
        sign = 1.0 if signal.direction == "LONG" else -1.0
        stop_distance = min(profile.stop_atr * atr, entry * profile.max_stop_pct / 100.0)
        stop = entry - sign * stop_distance
        take = entry + sign * stop_distance * profile.reward_r
        exit_price = float(path.iloc[-1].close)
        outcome = "TIME"
        exit_ms = int(path.index[-1]) + 3_600_000
        for timestamp, bar in path.iterrows():
            if sign > 0 and bar.open <= stop:
                exit_price = float(bar.open)
                outcome = "STOP_GAP"
                exit_ms = int(timestamp) + 3_600_000
                break
            if sign < 0 and bar.open >= stop:
                exit_price = float(bar.open)
                outcome = "STOP_GAP"
                exit_ms = int(timestamp) + 3_600_000
                break
            if sign > 0 and bar.open >= take:
                exit_price = take
                outcome = "TAKE_GAP"
                exit_ms = int(timestamp) + 3_600_000
                break
            if sign < 0 and bar.open <= take:
                exit_price = take
                outcome = "TAKE_GAP"
                exit_ms = int(timestamp) + 3_600_000
                break
            stop_hit = bar.low <= stop if sign > 0 else bar.high >= stop
            take_hit = bar.high >= take if sign > 0 else bar.low <= take
            if stop_hit:
                exit_price = stop
                outcome = "STOP"
                exit_ms = int(timestamp) + 3_600_000
                break
            if take_hit:
                exit_price = take
                outcome = "TAKE"
                exit_ms = int(timestamp) + 3_600_000
                break
        gross_pct = sign * (exit_price / entry - 1.0) * 100.0
        net_pct = gross_pct - cost_pct
        trades.append(
            {
                "profile": profile.name,
                "signal_ms": int(signal.available_ms),
                "entry_ms": int(signal.available_ms)
                + execution_delay_hours * 3_600_000,
                "exit_ms": exit_ms,
                "symbol": signal.symbol,
                "direction": signal.direction,
                "market_direction": signal.market_direction,
                "strength": float(signal.strength),
                "entry": entry,
                "exit": exit_price,
                "outcome": outcome,
                "gross_pct": gross_pct,
                "cost_pct": cost_pct,
                "net_pct": net_pct,
            }
        )
        next_available_ms = exit_ms
    return pd.DataFrame(trades)


def simulate_minute(
    signals: pd.DataFrame,
    minute_dir: Path,
    profile: Profile,
    execution_delay_minutes: int,
    cost_pct: float = COST_PCT,
) -> pd.DataFrame:
    cache: dict[str, pd.DataFrame] = {}
    trades: list[dict[str, Any]] = []
    next_available_ms = -1
    for signal in signals.itertuples(index=False):
        if signal.available_ms < next_available_ms:
            continue
        if signal.symbol not in cache:
            path = minute_dir / f"{signal.symbol}.parquet"
            if not path.exists():
                continue
            cache[signal.symbol] = (
                pd.read_parquet(
                    path,
                    columns=["open_time", "open", "high", "low", "close"],
                )
                .sort_values("open_time")
                .set_index("open_time")
            )
        scoped = cache[signal.symbol]
        entry_ms = int(signal.available_ms) + execution_delay_minutes * 60_000
        start = int(scoped.index.searchsorted(entry_ms))
        end_ms = entry_ms + profile.hold_hours * 3_600_000
        end = int(scoped.index.searchsorted(end_ms, side="left"))
        path = scoped.iloc[start:end]
        if path.empty or int(path.index[0]) != entry_ms:
            continue
        entry = float(path.iloc[0].open)
        atr = float(signal.atr_24h)
        if not np.isfinite(entry) or not np.isfinite(atr) or entry <= 0 or atr <= 0:
            continue
        sign = 1.0 if signal.direction == "LONG" else -1.0
        stop_distance = min(profile.stop_atr * atr, entry * profile.max_stop_pct / 100.0)
        stop = entry - sign * stop_distance
        take = entry + sign * stop_distance * profile.reward_r
        exit_price = float(path.iloc[-1].close)
        outcome = "TIME"
        exit_ms = int(path.index[-1]) + 60_000
        for timestamp, bar in path.iterrows():
            if sign > 0 and bar.open <= stop:
                exit_price, outcome = float(bar.open), "STOP_GAP"
            elif sign < 0 and bar.open >= stop:
                exit_price, outcome = float(bar.open), "STOP_GAP"
            elif sign > 0 and bar.open >= take:
                exit_price, outcome = take, "TAKE_GAP"
            elif sign < 0 and bar.open <= take:
                exit_price, outcome = take, "TAKE_GAP"
            else:
                stop_hit = bar.low <= stop if sign > 0 else bar.high >= stop
                take_hit = bar.high >= take if sign > 0 else bar.low <= take
                if stop_hit:
                    exit_price, outcome = stop, "STOP"
                elif take_hit:
                    exit_price, outcome = take, "TAKE"
                else:
                    continue
            exit_ms = int(timestamp) + 60_000
            break
        gross_pct = sign * (exit_price / entry - 1.0) * 100.0
        trades.append(
            {
                "profile": profile.name,
                "signal_ms": int(signal.available_ms),
                "entry_ms": entry_ms,
                "exit_ms": exit_ms,
                "symbol": signal.symbol,
                "direction": signal.direction,
                "market_direction": signal.market_direction,
                "strength": float(signal.strength),
                "entry": entry,
                "exit": exit_price,
                "outcome": outcome,
                "gross_pct": gross_pct,
                "cost_pct": cost_pct,
                "net_pct": gross_pct - cost_pct,
            }
        )
        next_available_ms = exit_ms
    return pd.DataFrame(trades)


def summarize(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {"trades": 0, "profit_factor": 0.0, "net_pct_points": 0.0}
    wins = frame.net_pct.clip(lower=0).sum()
    losses = -frame.net_pct.clip(upper=0).sum()
    cumulative = frame.net_pct.cumsum()
    drawdown = cumulative.cummax() - cumulative
    return {
        "trades": int(len(frame)),
        "symbols": int(frame.symbol.nunique()),
        "win_rate": round(float(frame.net_pct.gt(0).mean() * 100), 4),
        "profit_factor": round(float(wins / losses), 4) if losses > 0 else 999.0,
        "net_pct_points": round(float(frame.net_pct.sum()), 6),
        "mean_net_pct": round(float(frame.net_pct.mean()), 6),
        "max_drawdown_pct_points": round(float(drawdown.max()), 6),
        "long_trades": int(frame.direction.eq("LONG").sum()),
        "short_trades": int(frame.direction.eq("SHORT").sum()),
    }


def windows(frame: pd.DataFrame) -> dict[str, dict[str, Any]]:
    boundaries = {
        "development": ("2026-02-01", "2026-04-01"),
        "validation": ("2026-04-01", "2026-06-01"),
        "test": ("2026-06-01", "2026-07-01"),
        "final_july": ("2026-07-01", "2026-08-01"),
    }
    result: dict[str, dict[str, Any]] = {}
    timestamps = pd.to_datetime(frame.entry_ms, unit="ms", utc=True)
    for name, (start, end) in boundaries.items():
        mask = timestamps.ge(pd.Timestamp(start, tz="UTC")) & timestamps.lt(
            pd.Timestamp(end, tz="UTC")
        )
        result[name] = summarize(frame[mask])
    return result


def robustness(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {}
    cost_stress: dict[str, Any] = {}
    for cost in (0.18, 0.24, 0.36):
        stressed = frame.copy()
        stressed["net_pct"] = stressed.gross_pct - cost
        cost_stress[f"{cost:.2f}%"] = {
            "overall": summarize(stressed),
            "windows": windows(stressed),
        }
    contribution = frame.groupby("symbol").net_pct.sum().sort_values(ascending=False)
    leave_one_out = {
        symbol: round(float(frame.loc[frame.symbol.ne(symbol), "net_pct"].sum()), 6)
        for symbol in contribution.head(10).index
    }
    monthly_frame = frame.copy()
    monthly_frame["month"] = pd.to_datetime(
        monthly_frame.entry_ms,
        unit="ms",
        utc=True,
    ).dt.strftime("%Y-%m")
    return {
        "cost_stress": cost_stress,
        "top_positive_symbol_contributions": {
            symbol: round(float(value), 6)
            for symbol, value in contribution.head(10).items()
        },
        "worst_net_after_removing_one_top_symbol": round(
            float(min(leave_one_out.values())),
            6,
        ),
        "monthly": {
            month: summarize(scoped)
            for month, scoped in monthly_frame.groupby("month", sort=True)
        },
    }


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    cache = args.output / "hourly_panel.parquet"
    if cache.exists():
        panel = pd.read_parquet(cache)
    else:
        panel = build_panel(args.minute_dir, args.max_symbols)
        panel.to_parquet(cache, index=False)
    report: dict[str, Any] = {
        "method": (
            "Completed hourly bars, cross-sectional momentum, BTC plus breadth "
            "regime alignment, next-hour open entry, conservative stop-first OHLC "
            f"execution, {COST_PCT:.2f}% round-trip cost."
        ),
        "panel_rows": int(len(panel)),
        "symbols": int(panel.symbol.nunique()),
        "profiles": {},
    }
    all_trades: list[pd.DataFrame] = []
    for profile in PROFILES:
        signals = rank_signals(panel, profile)
        trades = simulate(signals, panel, profile)
        trades["execution_mode"] = "hourly"
        all_trades.append(trades)
        delayed = simulate(
            signals,
            panel,
            profile,
            execution_delay_hours=1,
        )
        minute_1m = simulate_minute(signals, args.minute_dir, profile, 1)
        minute_5m = simulate_minute(signals, args.minute_dir, profile, 5)
        minute_1m["execution_mode"] = "minute_delay_1m"
        minute_5m["execution_mode"] = "minute_delay_5m"
        all_trades.extend([minute_1m, minute_5m])
        report["profiles"][profile.name] = {
            "parameters": profile.__dict__,
            "overall": summarize(trades),
            "windows": windows(trades),
            "one_hour_execution_delay": {
                "overall": summarize(delayed),
                "windows": windows(delayed),
            },
            "minute_execution_1m_delay": {
                "overall": summarize(minute_1m),
                "windows": windows(minute_1m),
                "robustness": robustness(minute_1m),
            },
            "minute_execution_5m_delay": {
                "overall": summarize(minute_5m),
                "windows": windows(minute_5m),
                "robustness": robustness(minute_5m),
            },
        }
    pd.concat(all_trades, ignore_index=True).to_parquet(
        args.output / "trades.parquet",
        index=False,
    )
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
