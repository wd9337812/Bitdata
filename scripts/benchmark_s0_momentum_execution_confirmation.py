from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_s0_xmom_regime_candidates import block_bootstrap, scope  # noqa: E402
from scripts.benchmark_s0_cross_sectional_momentum import summarize  # noqa: E402


DEFAULT_RESEARCH = ROOT / "data" / "research" / "s0_point_in_time_slow_momentum"
DEFAULT_MINUTE_DIR = ROOT / "data" / "research" / "binance_um_event_1m" / "parquet"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_momentum_execution_confirmation"
BASE_COST = 0.24
STRESS_COST = 0.36
HOLD_MINUTES = 24 * 60


def cohort(symbol: str) -> str:
    """Stable symbol split, frozen independently of results."""
    bucket = int(hashlib.sha256(symbol.encode("utf-8")).hexdigest()[:8], 16) % 10
    return "research" if bucket < 6 else "validation" if bucket < 8 else "blind"


def _load_path(path: Path) -> pd.DataFrame:
    frame = pd.read_parquet(
        path,
        columns=[
            "open_time", "open", "high", "low", "close", "quote_volume",
            "taker_buy_quote_volume",
        ],
    ).sort_values("open_time")
    frame = frame.drop_duplicates("open_time").set_index("open_time")
    return frame


def _five_minute(frame: pd.DataFrame, start_ms: int) -> pd.DataFrame:
    scoped = frame.loc[(frame.index >= start_ms - 21 * 5 * 60_000) &
                       (frame.index < start_ms + 20 * 60_000)].copy()
    if scoped.empty:
        return pd.DataFrame()
    scoped["bucket"] = ((scoped.index - start_ms) // 300_000) * 300_000 + start_ms
    grouped = scoped.groupby("bucket", sort=True)
    result = grouped.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        quote_volume=("quote_volume", "sum"),
        taker_buy_quote_volume=("taker_buy_quote_volume", "sum"),
        minute_count=("close", "size"),
    )
    result["buy_ratio"] = result.taker_buy_quote_volume / result.quote_volume.replace(0, np.nan)
    result["ema8"] = result.close.ewm(span=8, adjust=False, min_periods=8).mean()
    result["ema21"] = result.close.ewm(span=21, adjust=False, min_periods=21).mean()
    return result


def _confirmed_entry(
    signal: Any,
    frame: pd.DataFrame,
) -> tuple[int, float] | None:
    start_ms = int(signal.available_ms)
    bars = _five_minute(frame, start_ms)
    if bars.empty:
        return None
    sign = 1 if signal.direction == "LONG" else -1
    for offset in (0, 300_000, 600_000):
        bucket = start_ms + offset
        if bucket not in bars.index or bucket + 300_000 not in bars.index:
            continue
        current = bars.loc[bucket]
        if int(current.minute_count) < 5:
            continue
        previous = bars.loc[:bucket].iloc[:-1]
        if len(previous) < 12:
            continue
        median_volume = float(previous.quote_volume.tail(12).median())
        volume_ok = float(current.quote_volume) >= median_volume * 0.5
        trend_ok = bool(current.ema8 > current.ema21) if sign > 0 else bool(current.ema8 < current.ema21)
        candle_ok = float(current.close) > float(current.open) if sign > 0 else float(current.close) < float(current.open)
        flow_ok = float(current.buy_ratio) >= 0.52 if sign > 0 else float(current.buy_ratio) <= 0.48
        if trend_ok and candle_ok and flow_ok and volume_ok:
            return bucket + 300_000, float(bars.loc[bucket + 300_000].open)
    return None


def _trade(signal: Any, frame: pd.DataFrame, cost_pct: float) -> dict[str, Any] | None:
    confirmed = _confirmed_entry(signal, frame)
    if confirmed is None:
        return None
    entry_ms, entry = confirmed
    end_ms = entry_ms + HOLD_MINUTES * 60_000
    path = frame.loc[(frame.index >= entry_ms) & (frame.index < end_ms)]
    if path.empty or not np.isfinite(entry) or entry <= 0:
        return None
    atr = float(signal.atr_24h)
    if not np.isfinite(atr) or atr <= 0:
        return None
    sign = 1.0 if signal.direction == "LONG" else -1.0
    stop_distance = min(2.0 * atr, entry * 0.15)
    stop = entry - sign * stop_distance
    take = entry + sign * stop_distance * 2.0
    exit_price = float(path.iloc[-1].close)
    outcome = "TIME"
    exit_ms = int(path.index[-1]) + 60_000
    for timestamp, bar in path.iterrows():
        if sign > 0 and bar.open <= stop or sign < 0 and bar.open >= stop:
            exit_price, outcome = float(bar.open), "STOP_GAP"
        elif sign > 0 and bar.open >= take or sign < 0 and bar.open <= take:
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
    return {
        "signal_ms": int(signal.available_ms),
        "entry_ms": entry_ms,
        "exit_ms": exit_ms,
        "confirmation_ms": entry_ms - 300_000,
        "symbol": signal.symbol,
        "cohort": cohort(signal.symbol),
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


def simulate(signals: pd.DataFrame, minute_dir: Path, cost_pct: float) -> pd.DataFrame:
    cache: dict[str, pd.DataFrame] = {}
    trades: list[dict[str, Any]] = []
    next_available_ms = -1
    for signal in signals.sort_values("available_ms").itertuples(index=False):
        if int(signal.available_ms) < next_available_ms:
            continue
        if signal.symbol not in cache:
            path = minute_dir / f"{signal.symbol}.parquet"
            if not path.exists():
                continue
            cache[signal.symbol] = _load_path(path)
        if not (float(signal.ret_24h) * float(signal.ret_72h) > 0):
            continue
        trade = _trade(signal, cache[signal.symbol], cost_pct)
        if trade is not None:
            trades.append(trade)
            next_available_ms = int(trade["exit_ms"])
    return pd.DataFrame(trades)


def _windows(frame: pd.DataFrame) -> dict[str, dict[str, Any]]:
    return {
        name: summarize(scope(frame, start, end))
        for name, start, end in (
            ("development", "2026-02-01", "2026-04-01"),
            ("validation_apr_may", "2026-04-01", "2026-06-01"),
            ("test_june", "2026-06-01", "2026-07-01"),
            ("final_july", "2026-07-01", "2026-08-01"),
        )
    }


def _report(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {"overall": summarize(frame), "windows": _windows(frame), "cohorts": {}}
    cohorts = {
        name: summarize(scoped)
        for name, scoped in frame.groupby("cohort", sort=True)
    }
    top = frame.groupby("symbol").net_pct.sum().nlargest(3).index
    without_top = frame.loc[~frame.symbol.isin(top)]
    return {
        "overall": summarize(frame),
        "windows": _windows(frame),
        "cohorts": cohorts,
        "without_top_3_symbols": summarize(without_top),
        "bootstrap": block_bootstrap(frame),
    }


def qualifies(reports: dict[str, dict[str, Any]]) -> bool:
    base = reports["base"]
    for window in ("validation_apr_may", "test_june", "final_july"):
        metrics = base["windows"][window]
        if metrics.get("trades", 0) < 8 or metrics.get("profit_factor", 0) <= 1.0 or metrics.get("net_pct_points", 0) <= 0:
            return False
    blind = base["cohorts"].get("blind", {})
    if blind.get("trades", 0) < 12 or blind.get("profit_factor", 0) <= 1.0 or blind.get("net_pct_points", 0) <= 0:
        return False
    if base["without_top_3_symbols"].get("net_pct_points", 0) <= 0:
        return False
    stress = reports["stress"]
    return stress["overall"].get("net_pct_points", 0) > 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark frozen momentum execution confirmation.")
    parser.add_argument("--research", type=Path, default=DEFAULT_RESEARCH)
    parser.add_argument("--minute-dir", type=Path, default=DEFAULT_MINUTE_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    signals = pd.read_parquet(args.research / "selected_signals.parquet")
    base_trades = simulate(signals, args.minute_dir, BASE_COST)
    stress_trades = simulate(signals, args.minute_dir, STRESS_COST)
    reports = {"base": _report(base_trades), "stress": _report(stress_trades)}
    base_trades.to_parquet(args.output / "trades_base.parquet", index=False)
    stress_trades.to_parquet(args.output / "trades_stress.parquet", index=False)
    result = {
        "experiment": "s0_momentum_execution_confirmation",
        "rule": "7d cross-sectional momentum + same-sign 24h/72h + 5m EMA8/21, candle, taker flow and volume confirmation; next 5m open",
        "signals": int(len(signals)),
        "reports": reports,
        "qualified_for_research_shadow": qualifies(reports),
        "decision": "qualified_for_research_shadow" if qualifies(reports) else "research_only_not_eligible",
    }
    (args.output / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
