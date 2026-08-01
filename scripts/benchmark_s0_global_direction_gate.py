from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "research" / "binance_um_prefunding_5m"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_global_direction_gate"
BASE_COST = 0.0024
STRESS_COST = 0.0036
LOOKBACKS = (30, 60, 90)
WINDOWS = {
    "development_2022_2023": ("2022-01-01", "2024-01-01"),
    "validation_2024": ("2024-01-01", "2025-01-01"),
    "test_2025": ("2025-01-01", "2026-01-01"),
    "blind_2026_h1": ("2026-01-01", "2026-07-01"),
}


def daily_panel() -> pd.DataFrame:
    parts = []
    symbols = sorted(
        directory.name
        for directory in (DATA / "klines").iterdir()
        if directory.is_dir() and list(directory.glob("*.parquet"))
    )
    for symbol in symbols:
        paths = sorted((DATA / "klines" / symbol).glob("*.parquet"))
        frame = pd.concat(
            [pd.read_parquet(path, columns=["open_time", "open", "high", "low", "close"]) for path in paths],
            ignore_index=True,
        )
        frame["time"] = pd.to_datetime(frame.open_time, unit="ms", utc=True)
        frame["day"] = frame.time.dt.floor("D")
        grouped = frame.groupby("day", sort=True)
        daily = grouped.agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            count=("close", "size"),
        )
        daily["symbol"] = symbol
        parts.append(daily.reset_index())
    result = pd.concat(parts, ignore_index=True).loc[lambda value: value["count"].eq(288)].copy()
    result["ret_14d"] = result.groupby("symbol").close.pct_change(14)
    true_range = pd.concat(
        [result.high - result.low, (result.high - result.close.groupby(result.symbol).shift()).abs(), (result.low - result.close.groupby(result.symbol).shift()).abs()],
        axis=1,
    ).max(axis=1)
    result["atr_14"] = true_range.groupby(result.symbol).transform(lambda values: values.rolling(14, min_periods=14).mean().shift(1))
    return result.sort_values(["day", "symbol"]).reset_index(drop=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit a global direction gate over daily cross-sectional momentum.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def _direction_stats(history: dict[str, list[tuple[pd.Timestamp, float]]], direction: str, day: pd.Timestamp, lookback: int) -> list[float]:
    cutoff = day - pd.Timedelta(days=lookback)
    return [value for timestamp, value in history[direction] if cutoff <= timestamp < day]


def _levels(series: pd.DataFrame, entry_index: int, direction: str) -> tuple[float, float]:
    entry = series.iloc[entry_index]
    atr = float(entry.atr_14) if pd.notna(entry.atr_14) and entry.atr_14 > 0 else float(entry.close) * 0.01
    risk = max(2.0 * atr, float(entry.open) * 0.01)
    if direction == "LONG":
        return float(entry.open) - risk, float(entry.open) + 3.0 * risk
    return float(entry.open) + risk, float(entry.open) - 3.0 * risk


def _exit(series: pd.DataFrame, entry_index: int, direction: str, stop: float, target: float) -> tuple[float, pd.Timestamp, str]:
    path = series.iloc[entry_index : entry_index + 4]
    for row in path.itertuples(index=False):
        if direction == "LONG":
            if row.open <= stop:
                return float(row.open), row.day, "STOP_GAP"
            if row.open >= target:
                return float(row.open), row.day, "TARGET_GAP"
            if row.low <= stop:
                return stop, row.day, "STOP"
            if row.high >= target:
                return target, row.day, "TARGET"
        else:
            if row.open >= stop:
                return float(row.open), row.day, "STOP_GAP"
            if row.open <= target:
                return float(row.open), row.day, "TARGET_GAP"
            if row.high >= stop:
                return stop, row.day, "STOP"
            if row.low <= target:
                return target, row.day, "TARGET"
    row = path.iloc[-1]
    return float(row.close), row.day, "TIME_EXIT"


def simulate(panel: pd.DataFrame, lookback: int, cost: float = STRESS_COST) -> pd.DataFrame:
    x = panel.copy()
    formation = "ret_14d"
    x["breadth"] = x.groupby("day")[formation].transform(lambda values: float((values > 0).mean()))
    btc = x.loc[x.symbol.eq("BTCUSDT"), ["day", formation]].rename(columns={formation: "btc_return"})
    x = x.merge(btc, on="day", how="left")
    x["market_direction"] = np.select(
        [x.breadth.ge(0.55) & x.btc_return.gt(0), x.breadth.le(0.45) & x.btc_return.lt(0)],
        ["LONG", "SHORT"],
        default="",
    )
    eligible = x.loc[x.market_direction.ne("") & x[formation].notna()].copy()
    eligible["strength"] = np.where(eligible.market_direction.eq("LONG"), eligible[formation], -eligible[formation])
    selected = eligible.sort_values(["day", "strength"], ascending=[True, False]).groupby("day").head(1)
    by_symbol = {symbol: group.sort_values("day").reset_index(drop=True) for symbol, group in x.groupby("symbol", sort=False)}
    day_values = sorted(x.day.unique())
    day_index = {day: index for index, day in enumerate(day_values)}
    history: dict[str, list[tuple[pd.Timestamp, float]]] = {"LONG": [], "SHORT": []}
    next_signal_index = 0
    trades: list[dict[str, Any]] = []
    for row in selected.sort_values("day").itertuples(index=False):
        signal_index = day_index[row.day]
        if signal_index < next_signal_index:
            continue
        prior = _direction_stats(history, row.market_direction, row.day, lookback)
        if len(prior) >= 10 and float(np.mean(prior)) < 0:
            continue
        series = by_symbol[row.symbol]
        matching = series.index[series.day.eq(row.day)]
        if len(matching) == 0:
            continue
        entry_index = int(matching[0]) + 1
        if entry_index + 3 >= len(series):
            continue
        entry = series.iloc[entry_index]
        stop, target = _levels(series, entry_index, row.market_direction)
        exit_price, exit_day, reason = _exit(series, entry_index, row.market_direction, stop, target)
        sign = 1.0 if row.market_direction == "LONG" else -1.0
        gross = sign * (exit_price / float(entry.open) - 1.0)
        net = gross - cost
        history[row.market_direction].append((exit_day, net))
        trades.append(
            {
                "signal_day": row.day,
                "entry_day": entry.day,
                "exit_day": exit_day,
                "symbol": row.symbol,
                "direction": row.market_direction,
                "gross_return": gross,
                "net_return": net,
                "exit_reason": reason,
                "prior_direction_samples": len(prior),
                "prior_direction_mean": float(np.mean(prior)) if prior else None,
            }
        )
        next_signal_index = signal_index + 4
    return pd.DataFrame(trades)


def metrics(trades: pd.DataFrame) -> dict[str, float | int]:
    if trades.empty:
        return {"trades": 0, "win_rate": 0.0, "profit_factor": 0.0, "net_return": 0.0, "max_drawdown": 0.0}
    returns = trades.net_return.astype(float)
    gains = float(returns[returns.gt(0)].sum())
    losses = float(-returns[returns.lt(0)].sum())
    curve = (1.0 + returns).cumprod()
    return {
        "trades": int(len(trades)),
        "win_rate": float(returns.gt(0).mean()),
        "profit_factor": gains / losses if losses > 0 else 999.0,
        "net_return": float(returns.sum()),
        "max_drawdown": float((curve / curve.cummax() - 1.0).min()),
    }


def scoped(trades: pd.DataFrame) -> dict[str, dict[str, float | int]]:
    return {
        name: metrics(trades.loc[trades.entry_day.ge(pd.Timestamp(start, tz="UTC")) & trades.entry_day.lt(pd.Timestamp(end, tz="UTC"))])
        for name, (start, end) in WINDOWS.items()
    }


def remove_top_three(trades: pd.DataFrame) -> dict[str, dict[str, float | int]]:
    return {
        name: metrics(
            group.drop(group.nlargest(min(3, len(group)), "net_return").index)
        )
        for name, (start, end) in WINDOWS.items()
        for group in [trades.loc[trades.entry_day.ge(pd.Timestamp(start, tz="UTC")) & trades.entry_day.lt(pd.Timestamp(end, tz="UTC"))]]
    }


def main() -> None:
    args = parse_args()
    panel = daily_panel()
    reports: dict[str, Any] = {}
    trades_by_name: dict[str, pd.DataFrame] = {}
    for lookback in LOOKBACKS:
        trades = simulate(panel, lookback)
        name = f"lookback_{lookback}d"
        reports[name] = {"lookback_days": lookback, "stress_windows": scoped(trades), "stress_without_top_3": remove_top_three(trades), "overall": metrics(trades)}
        trades_by_name[name] = trades
    development_best = max(reports, key=lambda name: (reports[name]["stress_windows"]["development_2022_2023"]["profit_factor"], reports[name]["stress_windows"]["development_2022_2023"]["net_return"]))
    result = {"method": "daily 14d cross-sectional momentum with point-in-time global direction gate", "cost": STRESS_COST, "reports": reports, "selected_from_development": development_best, "decision": "research_only_until_forward_review"}
    args.output.mkdir(parents=True, exist_ok=True)
    trades_by_name[development_best].to_parquet(args.output / "development_selected_trades.parquet", index=False)
    (args.output / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
