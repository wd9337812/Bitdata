from __future__ import annotations

import argparse
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

from scripts.benchmark_s0_adaptive_30d_momentum import (  # noqa: E402
    apply_event_time_gate,
)
from scripts.benchmark_s0_30d_confluence import (  # noqa: E402
    confirmations,
)

DEFAULT_SIGNALS = (
    ROOT / "data" / "research" / "s0_30d_momentum_adaptive_v2"
    / "signals_all.parquet"
)
DEFAULT_MINUTE_DIR = (
    ROOT / "data" / "research" / "binance_um_30d_all_event_1m" / "parquet"
)
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_30d_momentum_highlev"
MINUTE_MS = 60_000
HOUR_MS = 3_600_000


@dataclass(frozen=True)
class ExitProfile:
    name: str
    stop_atr: float | None  # None means fixed stop_pct
    stop_pct: float | None
    reward_r: float | None  # None means no fixed take-profit
    trail_trigger_r: float | None  # None means no trailing exit
    trail_dist_atr: float | None
    hold_hours: int
    max_stop_pct: float = 12.0


EXIT_PROFILES = (
    ExitProfile("frozen_2_5R_120h", 2.5, None, 2.5, None, None, 120),
    ExitProfile("frozen_1_5R_120h", 2.5, None, 1.5, None, None, 120),
    ExitProfile("trail_2_5A_1R_120h", 2.5, None, None, 1.0, 1.0, 120),
    ExitProfile("trail_2_5A_1R_48h", 2.5, None, None, 1.0, 1.0, 48),
    ExitProfile("trail_1_5A_1R_48h", 1.5, None, None, 1.0, 1.0, 48),
    ExitProfile("trail_1_0A_1R_24h", 1.0, None, None, 1.0, 1.0, 24),
    ExitProfile("fixed3_1R_24h", None, 3.0, None, 1.0, 1.0, 24),
    ExitProfile("fixed3_1R_48h", None, 3.0, None, 1.0, 1.0, 48),
    ExitProfile("frozen_1_0R_120h", 2.5, None, 1.0, None, None, 120),
    ExitProfile("frozen_0_75R_120h", 2.5, None, 0.75, None, None, 120),
    ExitProfile("fixed2_1R_24h", None, 2.0, None, 1.0, 1.0, 24),
    ExitProfile("fixed2_0_75R_24h", None, 2.0, 0.75, None, None, 24),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Account-level high-leverage eventization of the frozen 30d "
            "momentum acceleration+breadth candidate: full margin, 3x-10x, "
            "30% risk cap, 5U hard stop, serial single position, from 10U."
        )
    )
    parser.add_argument("--signals", type=Path, default=DEFAULT_SIGNALS)
    parser.add_argument("--minute-dir", type=Path, default=DEFAULT_MINUTE_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--delay", type=int, default=5)
    parser.add_argument("--cost-pct", type=float, default=0.60)
    parser.add_argument("--min-ret6h-pct", type=float, default=0.5)
    parser.add_argument("--min-breadth-pct", type=float, default=0.5)
    parser.add_argument("--min-ret24h-pct", type=float, default=5.0)
    parser.add_argument("--leverage", type=float, default=5.0)
    parser.add_argument("--risk-pct", type=float, default=30.0)
    parser.add_argument("--initial-equity", type=float, default=10.0)
    parser.add_argument("--hard-stop-equity", type=float, default=5.0)
    parser.add_argument("--margin-fraction", type=float, default=0.90)
    parser.add_argument("--maintenance-margin-pct", type=float, default=0.5)
    parser.add_argument("--max-trades", type=int, default=0)
    parser.add_argument("--bootstrap-samples", type=int, default=1_000)
    parser.add_argument("--bootstrap-seed", type=int, default=20260805)
    parser.add_argument("--profiles", nargs="*", default=None)
    parser.add_argument("--dev-end-year", type=int, default=2023)
    return parser.parse_args()


def load_minute(path: Path) -> pd.DataFrame | None:
    frame = pd.read_parquet(
        path,
        columns=["open_time", "open", "high", "low", "close"],
    )
    if frame.empty:
        return None
    frame = frame.sort_values("open_time").reset_index(drop=True)
    frame["open_time"] = frame["open_time"].astype("int64")
    for column in ("open", "high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna(subset=["open", "high", "low", "close"]).reset_index(
        drop=True
    )


def _stop_distance(
    entry: float,
    atr: float,
    profile: ExitProfile,
) -> float:
    if profile.stop_atr is not None:
        raw = profile.stop_atr * atr
        return min(raw, entry * profile.max_stop_pct / 100.0) / entry * 100.0
    return float(profile.stop_pct or 0.0)


def _simulate_minute_exit(
    frame: pd.DataFrame,
    entry_ms: int,
    entry: float,
    direction: int,
    stop_pct: float,
    atr: float,
    profile: ExitProfile,
) -> tuple[int, float, str, int] | None:
    """Adverse-first minute exit with optional fixed-R and trailing exits."""
    times = frame.open_time.to_numpy()
    highs = frame.high.to_numpy()
    lows = frame.low.to_numpy()
    closes = frame.close.to_numpy()
    start = int(times.searchsorted(entry_ms, side="left"))
    if start >= len(times):
        return None
    end = min(start + profile.hold_hours * 60, len(times))
    if end <= start:
        return None
    sign = float(direction)
    stop_dist = entry * stop_pct / 100.0
    stop = entry - sign * stop_dist
    target = (
        entry + sign * stop_dist * profile.reward_r
        if profile.reward_r is not None
        else None
    )
    trail_dist = (
        max(profile.trail_dist_atr * atr, stop_dist)
        if profile.trail_dist_atr is not None
        else None
    )
    trigger_dist = (
        stop_dist * profile.trail_trigger_r
        if profile.trail_trigger_r is not None
        else None
    )
    armed = False
    trail_stop: float | None = None
    best = entry
    for index in range(start, end):
        high = float(highs[index])
        low = float(lows[index])
        if sign > 0:
            if low <= stop:
                return int(times[index]), stop, "STOP", index - start + 1
            if trigger_dist is not None:
                if not armed and high >= entry + trigger_dist:
                    armed = True
                    best = max(best, high)
                    trail_stop = max(best - trail_dist, stop)
                elif armed:
                    best = max(best, high)
                    trail_stop = max(trail_stop, best - trail_dist)
                    if low <= trail_stop:
                        return int(times[index]), trail_stop, "TRAIL", index - start + 1
            if target is not None and high >= target:
                return int(times[index]), target, "TAKE", index - start + 1
        else:
            if high >= stop:
                return int(times[index]), stop, "STOP", index - start + 1
            if trigger_dist is not None:
                if not armed and low <= entry - trigger_dist:
                    armed = True
                    best = min(best, low)
                    trail_stop = min(best + trail_dist, stop)
                elif armed:
                    best = min(best, low)
                    trail_stop = min(trail_stop, best + trail_dist)
                    if high >= trail_stop:
                        return int(times[index]), trail_stop, "TRAIL", index - start + 1
            if target is not None and low <= target:
                return int(times[index]), target, "TAKE", index - start + 1
    return int(times[end - 1]), float(closes[end - 1]), "TIME", end - start


def paper_trades(
    signals: pd.DataFrame,
    minute_dir: Path,
    profile: ExitProfile,
    delay_minutes: int,
    cost_pct: float,
) -> pd.DataFrame:
    cache: dict[str, pd.DataFrame] = {}
    trades: list[dict[str, Any]] = []
    for signal in signals.itertuples(index=False):
        symbol = str(signal.symbol)
        if symbol not in cache:
            path = minute_dir / f"{symbol}.parquet"
            cache[symbol] = load_minute(path) if path.exists() else None
        frame = cache[symbol]
        if frame is None or frame.empty:
            continue
        entry_ms = int(signal.available_ms) + delay_minutes * MINUTE_MS
        start = int(frame.open_time.to_numpy().searchsorted(entry_ms, side="left"))
        if start >= len(frame) or int(frame.open_time.iloc[start]) != entry_ms:
            continue
        entry = float(frame.open.iloc[start])
        atr = float(signal.atr_24h)
        if not np.isfinite(entry) or not np.isfinite(atr) or entry <= 0 or atr <= 0:
            continue
        sign = 1.0 if str(signal.direction) == "LONG" else -1.0
        stop_pct = _stop_distance(entry, atr, profile)
        if stop_pct <= 0:
            continue
        exit_result = _simulate_minute_exit(
            frame, entry_ms, entry, int(sign), stop_pct, atr, profile
        )
        if exit_result is None:
            continue
        exit_ms, exit_price, outcome, hold = exit_result
        gross_pct = sign * (exit_price / entry - 1.0) * 100.0
        trades.append(
            {
                "signal_ms": int(signal.available_ms),
                "entry_ms": entry_ms,
                "exit_ms": exit_ms,
                "symbol": symbol,
                "direction": str(signal.direction),
                "market_direction": str(signal.market_direction),
                "strength": float(signal.strength),
                "atr_24h": float(signal.atr_24h),
                "entry": entry,
                "exit": exit_price,
                "outcome": outcome,
                "gross_pct": gross_pct,
                "cost_pct": cost_pct,
                "net_pct": gross_pct - cost_pct,
                "hold_minutes": int(hold),
            }
        )
    return pd.DataFrame(trades)


def account_trade(
    frame: pd.DataFrame,
    entry_ms: int,
    entry: float,
    direction: int,
    atr: float,
    profile: ExitProfile,
    stop_pct: float,
    leverage: float,
    cost_pct: float,
    liq_pct: float,
) -> dict[str, Any]:
    if stop_pct >= liq_pct:
        return {
            "exit_ms": entry_ms + MINUTE_MS,
            "exit_price": entry
            * (1.0 - direction * liq_pct / 100.0),
            "outcome": "LIQUIDATED",
            "hold_minutes": 1,
            "pnl_equity_pct": -100.0,
        }
    exit_result = _simulate_minute_exit(
        frame, entry_ms, entry, direction, stop_pct, atr, profile
    )
    if exit_result is None:
        return {
            "exit_ms": entry_ms + MINUTE_MS,
            "exit_price": entry,
            "outcome": "SKIPPED",
            "hold_minutes": 0,
            "pnl_equity_pct": 0.0,
        }
    exit_ms, exit_price, outcome, hold = exit_result
    gross_equity_pct = (
        direction * (exit_price / entry - 1.0) * 100.0 * leverage
    )
    cost_equity_pct = cost_pct * leverage
    return {
        "exit_ms": exit_ms,
        "exit_price": exit_price,
        "outcome": outcome,
        "hold_minutes": hold,
        "pnl_equity_pct": gross_equity_pct - cost_equity_pct,
    }


def simulate_account(
    funded: pd.DataFrame,
    minute_dir: Path,
    profile: ExitProfile,
    args: argparse.Namespace,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    cache: dict[str, pd.DataFrame] = {}
    ordered = funded.sort_values(["entry_ms", "strength"], ascending=[True, False])
    equity = float(args.initial_equity)
    hard_stop = float(args.hard_stop_equity)
    leverage = float(args.leverage)
    liq_pct = 100.0 / leverage - float(args.maintenance_margin_pct)
    curve: list[dict[str, Any]] = [
        {"time": 0, "equity": equity, "trade_id": 0, "symbol": ""}
    ]
    rows: list[dict[str, Any]] = []
    busy_until = -1
    stopped = False
    for sequence, row in enumerate(ordered.itertuples(index=False)):
        entry_ms = int(row.entry_ms)
        if entry_ms < busy_until:
            continue
        symbol = str(row.symbol)
        if symbol not in cache:
            path = minute_dir / f"{symbol}.parquet"
            cache[symbol] = load_minute(path) if path.exists() else None
        frame = cache[symbol]
        if frame is None or frame.empty:
            continue
        start = int(frame.open_time.to_numpy().searchsorted(entry_ms, side="left"))
        if start >= len(frame) or int(frame.open_time.iloc[start]) != entry_ms:
            continue
        entry = float(frame.open.iloc[start])
        atr = float(row.atr_24h)
        if not np.isfinite(entry) or entry <= 0:
            continue
        if not np.isfinite(atr) or atr <= 0:
            continue
        sign = 1.0 if str(row.direction) == "LONG" else -1.0
        full_stop_pct = _stop_distance(entry, atr, profile)
        effective_stop_pct = min(full_stop_pct, float(args.risk_pct) / leverage)
        result = account_trade(
            frame,
            entry_ms,
            entry,
            int(sign),
            atr,
            profile,
            effective_stop_pct,
            leverage,
            float(args.cost_pct),
            liq_pct,
        )
        pnl = float(result["pnl_equity_pct"])
        equity *= 1.0 + pnl / 100.0
        busy_until = int(result["exit_ms"])
        rows.append(
            {
                **row._asdict(),
                "entry": entry,
                "exit": float(result["exit_price"]),
                "outcome": str(result["outcome"]),
                "hold_minutes": int(result["hold_minutes"]),
                "pnl_equity_pct": round(pnl, 6),
                "effective_stop_pct": round(effective_stop_pct, 4),
                "equity_after": round(equity, 8),
            }
        )
        curve.append(
            {
                "time": int(result["exit_ms"]),
                "equity": round(equity, 8),
                "trade_id": len(rows),
                "symbol": symbol,
            }
        )
        if args.max_trades and len(rows) >= args.max_trades:
            break
        if equity <= hard_stop:
            stopped = True
            break
    return pd.DataFrame(rows), pd.DataFrame(curve)


def account_metrics(
    rows: pd.DataFrame,
    curve: pd.DataFrame,
    args: argparse.Namespace,
) -> dict[str, Any]:
    if rows.empty:
        return {"trades": 0, "final_equity": args.initial_equity}
    equity = curve.equity.to_numpy(dtype=float)
    peak = np.maximum.accumulate(equity)
    drawdown = (peak - equity) / peak
    pnl = rows.pnl_equity_pct.astype(float).to_numpy()
    wins = pnl[pnl > 0].sum()
    losses = -pnl[pnl < 0].sum()
    final = float(equity[-1])
    reach_time = None
    for index, row in curve.iterrows():
        if float(row.equity) >= float(args.initial_equity) * 1000.0:
            reach_time = int(row.time)
            break
    return {
        "trades": int(len(rows)),
        "symbols": int(rows.symbol.nunique()),
        "win_rate_pct": round(float((pnl > 0).mean() * 100.0), 4),
        "profit_factor": round(
            float(wins / losses) if losses > 0 else (999.0 if wins > 0 else 0.0),
            4,
        ),
        "final_equity": round(final, 8),
        "multiplier": round(final / float(args.initial_equity), 6),
        "max_drawdown_pct": round(float(drawdown.max() * 100.0), 4),
        "ruined": bool(final <= float(args.hard_stop_equity)),
        "reached_10000u": bool(reach_time is not None),
        "reach_ms": reach_time,
        "liquidation_count": int((rows.outcome == "LIQUIDATED").sum()),
        "stop_count": int((rows.outcome == "STOP").sum()),
        "trail_count": int((rows.outcome == "TRAIL").sum()),
        "take_count": int((rows.outcome == "TAKE").sum()),
        "time_count": int((rows.outcome == "TIME").sum()),
    }


def bootstrap_account(
    rows: pd.DataFrame,
    args: argparse.Namespace,
) -> dict[str, Any]:
    if rows.empty:
        return {"samples": 0}
    symbols = sorted(rows.symbol.unique())
    blocks = {
        symbol: rows.loc[rows.symbol.eq(symbol)].reset_index(drop=True)
        for symbol in symbols
    }
    rng = np.random.default_rng(args.bootstrap_seed)
    finals = np.empty(args.bootstrap_samples, dtype=float)
    ruined = np.empty(args.bootstrap_samples, dtype=bool)
    reached = np.empty(args.bootstrap_samples, dtype=bool)
    for sample in range(args.bootstrap_samples):
        chosen = rng.integers(0, len(symbols), size=len(symbols))
        sample_rows = pd.concat(
            [blocks[symbols[index]] for index in chosen],
            ignore_index=True,
        )
        sample_rows = sample_rows.sort_values(
            ["entry_ms", "strength"], ascending=[True, False]
        ).reset_index(drop=True)
        # Re-run account compounding on the resampled trade outcomes. The
        # minute-level re-simulation is deterministic given the same trade
        # parameters, so we replay the recorded equity PnLs.
        equity = float(args.initial_equity)
        busy_until = -1
        for trade in sample_rows.itertuples(index=False):
            entry_ms = int(trade.entry_ms)
            if entry_ms < busy_until:
                continue
            equity *= 1.0 + float(trade.pnl_equity_pct) / 100.0
            busy_until = int(trade.exit_ms)
            if equity <= 0:
                break
        finals[sample] = equity
        ruined[sample] = equity <= float(args.hard_stop_equity)
        reached[sample] = equity >= float(args.initial_equity) * 1000.0
    return {
        "samples": int(args.bootstrap_samples),
        "symbol_blocks": len(symbols),
        "p10_equity": round(float(np.quantile(finals, 0.10)), 6),
        "p50_equity": round(float(np.quantile(finals, 0.50)), 6),
        "p90_equity": round(float(np.quantile(finals, 0.90)), 6),
        "ruin_probability": round(float(ruined.mean()), 4),
        "reached_10000u_probability": round(float(reached.mean()), 4),
    }


def main() -> None:
    args = parse_args()
    signals = pd.read_parquet(args.signals)
    signals = confirmations(signals, args)
    signals = signals.loc[
        signals.acceleration & signals.breadth
    ].reset_index(drop=True)
    profiles = [
        profile
        for profile in EXIT_PROFILES
        if args.profiles is None or profile.name in args.profiles
    ]
    report: dict[str, Any] = {
        "experiment": "s0_30d_momentum_highlev",
        "signals_after_frozen_filter": int(len(signals)),
        "delay_minutes": args.delay,
        "cost_pct": args.cost_pct,
        "leverage": args.leverage,
        "risk_pct": args.risk_pct,
        "initial_equity": args.initial_equity,
        "hard_stop_equity": args.hard_stop_equity,
        "profiles": {},
    }
    year = pd.to_datetime(signals.available_ms, unit="ms", utc=True).dt.year
    signals = signals.assign(year=year)
    dev_signals = signals.loc[signals.year.le(args.dev_end_year)].reset_index(
        drop=True
    )
    oos_signals = signals.loc[signals.year.gt(args.dev_end_year)].reset_index(
        drop=True
    )
    for profile in profiles:
        paper = paper_trades(
            signals, args.minute_dir, profile, args.delay, args.cost_pct
        )
        if paper.empty:
            report["profiles"][profile.name] = {"paper_trades": 0}
            continue
        funded = apply_event_time_gate(
            paper, symbol_embargo_hours=72
        ).reset_index(drop=True)
        funded_year = pd.to_datetime(
            funded.entry_ms, unit="ms", utc=True
        ).dt.year
        funded = funded.assign(year=funded_year)
        dev_funded = funded.loc[funded.year.le(args.dev_end_year)].reset_index(
            drop=True
        )
        oos_funded = funded.loc[funded.year.gt(args.dev_end_year)].reset_index(
            drop=True
        )
        dev_rows, dev_curve = simulate_account(
            dev_funded, args.minute_dir, profile, args
        )
        oos_rows, oos_curve = simulate_account(
            oos_funded, args.minute_dir, profile, args
        )
        full_rows, full_curve = simulate_account(
            funded, args.minute_dir, profile, args
        )
        report["profiles"][profile.name] = {
            "paper_trades": int(len(paper)),
            "paper_win_rate_pct": round(
                float((paper.net_pct > 0).mean() * 100.0), 2
            ),
            "funded_trades": int(len(funded)),
            "funded_symbols": int(funded.symbol.nunique()),
            "dev": account_metrics(dev_rows, dev_curve, args),
            "oos_fresh": account_metrics(oos_rows, oos_curve, args),
            "full_path": account_metrics(full_rows, full_curve, args),
            "bootstrap_full": bootstrap_account(
                full_rows, args
            ),
        }
        print(
            f"{profile.name:22s} funded={len(funded):3d} "
            f"dev_final={report['profiles'][profile.name]['dev'].get('final_equity', 0):.2f} "
            f"oos_final={report['profiles'][profile.name]['oos_fresh'].get('final_equity', 0):.2f} "
            f"full_final={report['profiles'][profile.name]['full_path'].get('final_equity', 0):.2f} "
            f"ruin={report['profiles'][profile.name]['full_path'].get('ruined', False)}",
            flush=True,
        )
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
