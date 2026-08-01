from __future__ import annotations

import argparse
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data" / "research" / "binance_um_point_in_time_1h_2020_2023"
DEFAULT_VALIDATION = ROOT / "data" / "research" / "binance_um_point_in_time_1h_2024_2025"
DEFAULT_TEST = ROOT / "data" / "research" / "binance_um_point_in_time_1h"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_weekly_reversal"
BASE_ROUND_TRIP = 0.0012
STRESS_ROUND_TRIP = 0.0024


@dataclass(frozen=True)
class Parameters:
    formation_days: int
    skip_days: int
    quantile: float
    stop_pct: float
    high_vol_filter: bool = True

    @property
    def name(self) -> str:
        return f"f{self.formation_days}_s{self.skip_days}_q{self.quantile:.2f}_sl{self.stop_pct:.2f}"


# Frozen before reading this dataset.  The paper reports 8/10-week formation,
# quantile robustness, and stronger results in volatile mid-cap assets, but its
# full holding-period implementation is not available behind SSRN's gate.
PARAMETER_GRID = tuple(
    Parameters(formation, skip, quantile, stop)
    for formation in (56, 70)
    for skip in (0, 7)
    for quantile in (0.10, 0.20)
    for stop in (0.10, 0.20, 0.30)
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit a point-in-time weekly crypto reversal proxy.")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--validation", type=Path, default=DEFAULT_VALIDATION)
    parser.add_argument("--test", type=Path, default=DEFAULT_TEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workers", type=int, default=4)
    return parser.parse_args()


def iter_paths(roots: Iterable[Path]) -> list[Path]:
    return sorted({p.name: p for root in roots for p in (root / "parquet").glob("*.parquet")}.values(), key=lambda p: p.name)


def load_daily(path: Path, roots: Iterable[Path]) -> pd.DataFrame:
    cols = ["symbol", "open_time", "open", "high", "low", "close", "quote_volume"]
    parts = [pd.read_parquet(path, columns=cols)]
    for root in roots:
        extra = root / "parquet" / path.name
        if extra.exists() and extra != path:
            parts.append(pd.read_parquet(extra, columns=cols))
    hourly = pd.concat(parts, ignore_index=True).drop_duplicates("open_time").sort_values("open_time")
    hourly["time"] = pd.to_datetime(hourly.open_time, unit="ms", utc=True)
    daily = (hourly.set_index("time").resample("1D", label="left", closed="left")
             .agg(symbol=("symbol", "first"), open=("open", "first"), high=("high", "max"),
                  low=("low", "min"), close=("close", "last"), quote_volume=("quote_volume", "sum"),
                  hourly_rows=("open_time", "count")).dropna(subset=["symbol", "open", "high", "low", "close"]).reset_index())
    return daily.loc[daily.hourly_rows.eq(24)].drop(columns="hourly_rows").reset_index(drop=True)


def indicators(frame: pd.DataFrame, params: Parameters) -> pd.DataFrame:
    result = frame.copy().sort_values("time")
    result["formation_return"] = result.close.pct_change(params.formation_days).shift(params.skip_days)
    result["volatility"] = result.close.pct_change().rolling(30, min_periods=20).std().shift(1)
    result["liquidity_30d"] = result.quote_volume.rolling(30, min_periods=20).sum().shift(1)
    result["entry_open_1d"] = result.open.shift(-1)
    result["exit_close_7d"] = result.close.shift(-7)
    result["eligible"] = result.liquidity_30d.ge(25_000_000) & result.volatility.gt(0)
    return result


def build_panel(daily: pd.DataFrame, params: Parameters) -> pd.DataFrame:
    pieces = [
        indicators(frame, params)
        for _, frame in daily.groupby("symbol", sort=False)
        if len(frame) >= params.formation_days + params.skip_days + 45
    ]
    return pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()


def signals(panel: pd.DataFrame, params: Parameters) -> pd.DataFrame:
    if panel.empty:
        return panel.copy()
    candidates = panel.loc[panel.eligible & panel.time.dt.dayofweek.eq(0)].copy()
    if candidates.empty:
        return candidates
    selected: list[pd.DataFrame] = []
    for time, day in candidates.groupby("time", sort=True):
        cutoff = day.volatility.median()
        day = day.loc[day.volatility.ge(cutoff) if params.high_vol_filter else day.index.isin(day.index)]
        if len(day) < 10:
            continue
        day["rank"] = day.formation_return.rank(pct=True, method="first")
        long_side = day.loc[day["rank"].le(params.quantile)].copy()
        short_side = day.loc[day["rank"].ge(1 - params.quantile)].copy()
        if long_side.empty or short_side.empty:
            continue
        long_side["direction"] = "LONG"
        short_side["direction"] = "SHORT"
        combined = pd.concat([long_side, short_side])
        combined["strength"] = (combined["rank"] - 0.5).abs()
        selected.append(combined)
    return pd.concat(selected, ignore_index=True) if selected else pd.DataFrame()


def _path_exit(path: pd.DataFrame, direction: str, stop_pct: float) -> tuple[float, pd.Timestamp, str]:
    entry = float(path.iloc[0].open)
    stop = entry * (1 - stop_pct) if direction == "LONG" else entry * (1 + stop_pct)
    for timestamp, row in zip(path.index, path.itertuples(index=False)):
        if direction == "LONG" and float(row.open) <= stop:
            return float(row.open), timestamp, "STOP_GAP"
        if direction == "SHORT" and float(row.open) >= stop:
            return float(row.open), timestamp, "STOP_GAP"
        if direction == "LONG" and float(row.low) <= stop:
            return stop, timestamp, "STOP"
        if direction == "SHORT" and float(row.high) >= stop:
            return stop, timestamp, "STOP"
    return float(path.iloc[-1].close), path.index[-1], "TIME"


def simulate_s0(panel: pd.DataFrame, selected: pd.DataFrame, params: Parameters, cost: float) -> pd.DataFrame:
    if selected.empty:
        return pd.DataFrame()
    by_symbol = {s: f.sort_values("time").set_index("time") for s, f in panel.groupby("symbol", sort=False)}
    trades: list[dict[str, Any]] = []
    next_available = pd.Timestamp.min.tz_localize("UTC")
    auction = (
        selected.sort_values(
            ["time", "strength", "volatility", "liquidity_30d"],
            ascending=[True, False, False, False],
        )
        .groupby("time", sort=False)
        .head(1)
    )
    for row in auction.itertuples(index=False):
        entry_day = row.time + pd.Timedelta(days=1)
        if entry_day < next_available or row.symbol not in by_symbol:
            continue
        frame = by_symbol[row.symbol]
        after = frame.loc[frame.index >= entry_day].iloc[:7]
        if len(after) < 7:
            continue
        exit_price, exit_time, reason = _path_exit(after, row.direction, params.stop_pct)
        entry_price = float(after.iloc[0].open)
        sign = 1 if row.direction == "LONG" else -1
        gross = sign * (exit_price / entry_price - 1)
        trades.append({"symbol": row.symbol, "signal_time": row.time, "entry_time": after.index[0],
                       "exit_time": exit_time, "direction": row.direction, "parameters": params.name,
                       "gross_return": gross, "net_return": gross - cost, "exit_reason": reason})
        next_available = exit_time
    return pd.DataFrame(trades)


def simulate_portfolio(
    panel: pd.DataFrame,
    selected: pd.DataFrame,
    params: Parameters,
    cost: float,
) -> pd.DataFrame:
    if selected.empty:
        return pd.DataFrame()
    legs = selected.loc[
        selected.entry_open_1d.gt(0) & selected.exit_close_7d.gt(0)
    ].copy()
    sign = np.where(legs.direction.eq("LONG"), 1.0, -1.0)
    legs["net_return"] = sign * (
        legs.exit_close_7d / legs.entry_open_1d - 1.0
    ) - cost
    weekly = (
        legs.groupby("time", as_index=False)
        .agg(net_return=("net_return", "mean"), legs=("symbol", "count"))
        .rename(columns={"time": "entry_time"})
    )
    weekly["entry_time"] = weekly.entry_time + pd.Timedelta(days=1)
    weekly["parameters"] = params.name
    return weekly


def metrics(trades: pd.DataFrame) -> dict[str, float | int]:
    if trades.empty:
        return {"trades": 0, "win_rate": 0.0, "profit_factor": 0.0, "net_return": 0.0, "max_drawdown": 0.0}
    returns = trades.net_return.astype(float)
    gains = returns[returns.gt(0)].sum()
    losses = -returns[returns.lt(0)].sum()
    curve = (1 + returns).cumprod()
    drawdown = (curve / curve.cummax() - 1).min()
    return {"trades": int(len(trades)), "win_rate": float(returns.gt(0).mean()),
            "profit_factor": float(gains / losses) if losses > 0 else 999.0,
            "net_return": float(returns.sum()), "max_drawdown": float(drawdown)}


def windows(trades: pd.DataFrame) -> dict[str, dict[str, float | int]]:
    ranges = {"development_2021_2023": ("2021-01-01", "2024-01-01"), "validation_2024": ("2024-01-01", "2025-01-01"),
              "test_2025": ("2025-01-01", "2026-01-01"), "blind_2026_h1": ("2026-01-01", "2026-07-01")}
    return {name: metrics(trades.loc[trades.entry_time.ge(pd.Timestamp(start, tz="UTC")) & trades.entry_time.lt(pd.Timestamp(end, tz="UTC"))])
            for name, (start, end) in ranges.items()}


def main() -> None:
    args = parse_args()
    roots = [p for p in (args.data, args.validation, args.test) if p is not None]
    paths = iter_paths(roots)
    parts: list[pd.DataFrame] = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(load_daily, path, roots) for path in paths]
        for i, future in enumerate(as_completed(futures), 1):
            result = future.result()
            if not result.empty:
                parts.append(result)
            if i % 50 == 0 or i == len(futures):
                print(f"simulated {i}/{len(futures)} symbols", flush=True)
    daily = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    all_trade_parts: list[pd.DataFrame] = []
    all_portfolio_parts: list[pd.DataFrame] = []
    panel_cache: dict[tuple[int, int], pd.DataFrame] = {}
    signal_cache: dict[tuple[int, int, float], pd.DataFrame] = {}
    for params in PARAMETER_GRID:
        panel_key = (params.formation_days, params.skip_days)
        if panel_key not in panel_cache:
            panel_cache[panel_key] = build_panel(daily, params)
        signal_key = (*panel_key, params.quantile)
        if signal_key not in signal_cache:
            signal_cache[signal_key] = signals(panel_cache[panel_key], params)
        trades = simulate_s0(
            panel_cache[panel_key],
            signal_cache[signal_key],
            params,
            BASE_ROUND_TRIP,
        )
        if not trades.empty:
            all_trade_parts.append(trades)
        portfolio = simulate_portfolio(
            panel_cache[panel_key],
            signal_cache[signal_key],
            params,
            BASE_ROUND_TRIP,
        )
        if not portfolio.empty:
            all_portfolio_parts.append(portfolio)
    all_trades = pd.concat(all_trade_parts, ignore_index=True) if all_trade_parts else pd.DataFrame()
    all_portfolios = pd.concat(all_portfolio_parts, ignore_index=True) if all_portfolio_parts else pd.DataFrame()
    reports: dict[str, Any] = {}
    for params in PARAMETER_GRID:
        subset = all_trades.loc[all_trades.parameters.eq(params.name)] if not all_trades.empty else all_trades
        stress = subset.copy()
        if not stress.empty:
            stress["net_return"] = stress.gross_return - STRESS_ROUND_TRIP
        portfolio = all_portfolios.loc[all_portfolios.parameters.eq(params.name)] if not all_portfolios.empty else all_portfolios
        portfolio_stress = portfolio.copy()
        if not portfolio_stress.empty:
            portfolio_stress["net_return"] = portfolio_stress.net_return - STRESS_ROUND_TRIP + BASE_ROUND_TRIP
        reports[params.name] = {"parameters": asdict(params), "base": windows(subset), "stress": windows(stress),
                                "portfolio_base": windows(portfolio), "portfolio_stress": windows(portfolio_stress)}
    qualified = []
    for name, report in reports.items():
        stress_windows = report["stress"]
        if all(v["trades"] >= 8 and v["net_return"] > 0 and v["profit_factor"] > 1.05 for v in stress_windows.values()):
            qualified.append(name)
        portfolio_windows = report["portfolio_stress"]
        report["portfolio_qualified"] = bool(
            all(v["trades"] >= 8 and v["net_return"] > 0 and v["profit_factor"] > 1.05 for v in portfolio_windows.values())
        )
    report = {"method": "PIT weekly 8/10-week cross-sectional reversal proxy; single non-overlapping S0 position",
              "reference": "Kiefer & Nowotny (2026), SSRN 6703978",
              "proxy_limits": ["Paper full text was unavailable behind SSRN challenge.", "Point-in-time quote volume proxies mid-cap filtering.", "Seven-day hold is a preregistered executable proxy, not a claim about the paper's undisclosed implementation."],
              "parameter_grid": [asdict(p) for p in PARAMETER_GRID], "symbols": len(paths), "trades": int(len(all_trades)),
              "candidates": reports, "qualified": qualified,
              "decision": "reject_before_shadow_or_live" if not qualified else "eligible_for_independent_shadow"}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not all_trades.empty:
        all_trades.to_parquet(args.output / "trades.parquet", index=False)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
