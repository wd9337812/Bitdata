from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_s0_market_tsmom_28d import (
    DEFAULT_DATA,
    STRESS_ONE_WAY_COST,
    build_daily_panel,
    market_state,
)
from scripts.benchmark_s0_market_tsmom_consensus import consensus_state


DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_market_tsmom_trailing"
TRAIN_END_YEAR = 2023
SLOW_LOOKBACK = 56


@dataclass(frozen=True)
class Variant:
    atr_days: int
    atr_multiple: float
    disaster_stop_pct: float
    max_hold_days: int

    @property
    def name(self) -> str:
        return (
            f"atr{self.atr_days}x{self.atr_multiple:g}"
            f"_stop{int(self.disaster_stop_pct * 100):02d}"
            f"_hold{self.max_hold_days}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit a volatility-trailing exit for market consensus BTC trend."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def btc_daily(panel: pd.DataFrame) -> pd.DataFrame:
    frame = panel.loc[
        panel.symbol.eq("BTCUSDT"), ["day", "open", "close"]
    ].drop_duplicates("day", keep="last")
    high_low_parts = []
    for root in DEFAULT_DATA:
        path = root / "parquet" / "BTCUSDT.parquet"
        if not path.exists():
            continue
        hourly = pd.read_parquet(
            path, columns=["open_time", "high", "low"]
        )
        hourly["day"] = pd.to_datetime(
            hourly.open_time, unit="ms", utc=True
        ).dt.floor("D")
        high_low_parts.append(
            hourly.groupby("day", as_index=False).agg(
                high=("high", "max"), low=("low", "min")
            )
        )
    high_low = (
        pd.concat(high_low_parts, ignore_index=True)
        .drop_duplicates("day", keep="last")
        .sort_values("day")
    )
    frame = frame.merge(high_low, on="day", how="inner").sort_values("day")
    previous_close = frame.close.shift(1)
    frame["true_range"] = pd.concat(
        [
            frame.high - frame.low,
            (frame.high - previous_close).abs(),
            (frame.low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    for days in (10, 14, 20):
        frame[f"atr_{days}"] = frame.true_range.rolling(
            days, min_periods=days
        ).mean()
    return frame.reset_index(drop=True)


def simulate(
    btc: pd.DataFrame,
    state: pd.DataFrame,
    variant: Variant,
    *,
    execution_delay_days: int = 0,
) -> pd.DataFrame:
    frame = btc.merge(state[["day", "signal"]], on="day", how="left")
    frame["signal"] = frame.signal.fillna(False)
    rows: list[dict[str, Any]] = []
    index = 0
    while index + 1 + execution_delay_days < len(frame):
        if not bool(frame.iloc[index].signal):
            index += 1
            continue
        entry_index = index + 1 + execution_delay_days
        entry = frame.iloc[entry_index]
        entry_price = float(entry.open)
        atr = float(frame.iloc[index][f"atr_{variant.atr_days}"])
        if not math.isfinite(atr) or atr <= 0 or entry_price <= 0:
            index += 1
            continue
        disaster_stop = entry_price * (1.0 - variant.disaster_stop_pct)
        trailing_stop = max(
            disaster_stop,
            float(frame.iloc[index].close) - variant.atr_multiple * atr,
        )
        exit_index = min(
            entry_index + variant.max_hold_days,
            len(frame) - 1,
        )
        exit_price = float(frame.iloc[exit_index].open)
        reason = "max_hold"
        peak_close = float(frame.iloc[index].close)
        for path_index in range(entry_index, exit_index + 1):
            bar = frame.iloc[path_index]
            if float(bar.open) <= trailing_stop:
                exit_index = path_index
                exit_price = float(bar.open)
                reason = "gap_trailing_stop"
                break
            if float(bar.low) <= trailing_stop:
                exit_index = path_index
                exit_price = trailing_stop
                reason = "trailing_stop"
                break
            if path_index > entry_index and not bool(
                frame.iloc[path_index - 1].signal
            ):
                exit_index = path_index
                exit_price = float(bar.open)
                reason = "market_signal_off"
                break
            peak_close = max(peak_close, float(bar.close))
            path_atr = float(bar[f"atr_{variant.atr_days}"])
            if math.isfinite(path_atr) and path_atr > 0:
                trailing_stop = max(
                    trailing_stop,
                    peak_close - variant.atr_multiple * path_atr,
                )
        exit = frame.iloc[exit_index]
        rows.append(
            {
                "variant": variant.name,
                "signal_day": frame.iloc[index].day,
                "entry_day": entry.day,
                "exit_day": exit.day,
                "entry_price": entry_price,
                "initial_stop": trailing_stop,
                "initial_stop_pct": max(
                    0.0,
                    (entry_price - trailing_stop) / entry_price,
                ),
                "exit_price": exit_price,
                "gross_return": exit_price / entry_price - 1.0,
                "net_return": (
                    exit_price / entry_price
                    - 1.0
                    - 2.0 * STRESS_ONE_WAY_COST
                ),
                "exit_reason": reason,
                "hold_days": int(exit_index - entry_index),
            }
        )
        index = max(index + 1, exit_index)
    return pd.DataFrame(rows)


def metrics(
    trades: pd.DataFrame,
    *,
    equity_risk: float = 0.10,
    stop_pct: float = 0.10,
) -> dict[str, float | int]:
    if trades.empty:
        return {
            "trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "net_return": 0.0,
            "max_drawdown": 0.0,
        }
    leverage = min(3.0, equity_risk / stop_pct)
    net = trades.net_return.astype(float)
    wins = float(net.clip(lower=0).sum())
    losses = float(-net.clip(upper=0).sum())
    equity_returns = net * leverage
    equity = (1.0 + equity_returns).cumprod()
    drawdown = equity / equity.cummax() - 1.0
    return {
        "trades": int(len(trades)),
        "win_rate": round(float(net.gt(0).mean() * 100.0), 4),
        "profit_factor": round(wins / losses, 4) if losses else 999.0,
        "net_return": round(float(equity.iloc[-1] - 1.0), 6),
        "max_drawdown": round(float(drawdown.min()), 6),
        "average_hold_days": round(float(trades.hold_days.mean()), 4),
        "leverage": round(float(leverage), 4),
    }


def split_report(trades: pd.DataFrame, variant: Variant) -> dict[str, Any]:
    years = pd.to_datetime(trades.entry_day, utc=True).dt.year
    return {
        "training": metrics(
            trades.loc[years.le(TRAIN_END_YEAR)],
            stop_pct=variant.disaster_stop_pct,
        ),
        "out_of_sample": metrics(
            trades.loc[years.gt(TRAIN_END_YEAR)],
            stop_pct=variant.disaster_stop_pct,
        ),
        "annual": {
            str(int(year)): metrics(
                group,
                stop_pct=variant.disaster_stop_pct,
            )
            for year, group in trades.assign(year=years).groupby("year", sort=True)
        },
    }


def training_score(report: dict[str, Any]) -> tuple[int, float, float, float]:
    annual = report["annual"]
    years = [annual.get(str(year), {}) for year in range(2020, 2024)]
    positive_years = sum(float(item.get("net_return", 0)) > 0 for item in years)
    worst_year = min(float(item.get("net_return", -1)) for item in years)
    overall = report["training"]
    return (
        positive_years,
        worst_year,
        float(overall.get("profit_factor", 0)),
        float(overall.get("net_return", -1)),
    )


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    panel = build_daily_panel(DEFAULT_DATA)
    state = consensus_state(market_state(panel), SLOW_LOOKBACK)
    btc = btc_daily(panel)
    variants = [
        Variant(atr, multiple, stop, hold)
        for atr in (10, 14, 20)
        for multiple in (2.0, 3.0, 4.0)
        for stop in (0.10, 0.15)
        for hold in (10, 20, 30)
    ]
    reports: dict[str, Any] = {}
    trades_by_variant: dict[str, pd.DataFrame] = {}
    for variant in variants:
        trades = simulate(btc, state, variant)
        trades_by_variant[variant.name] = trades
        reports[variant.name] = {
            "parameters": asdict(variant),
            **split_report(trades, variant),
        }
    selected_name = max(reports, key=lambda name: training_score(reports[name]))
    selected = next(item for item in variants if item.name == selected_name)
    selected_trades = trades_by_variant[selected_name]
    delay_oos = {
        str(delay): split_report(
            simulate(btc, state, selected, execution_delay_days=delay),
            selected,
        )["out_of_sample"]
        for delay in (0, 1, 2)
    }
    risk_sweep = {
        str(risk): metrics(
            selected_trades,
            equity_risk=risk,
            stop_pct=selected.disaster_stop_pct,
        )
        for risk in (0.05, 0.10, 0.15, 0.20, 0.30)
    }
    selected_report = reports[selected_name]
    annual = selected_report["annual"]
    accepted = bool(
        selected_report["training"]["profit_factor"] > 1.10
        and selected_report["out_of_sample"]["profit_factor"] > 1.10
        and all(
            float(annual.get(str(year), {}).get("net_return", -1)) > 0
            for year in range(2024, 2027)
        )
        and all(
            result["profit_factor"] > 1.0 and result["net_return"] > 0
            for result in delay_oos.values()
        )
    )
    output = {
        "experiment": "s0_market_tsmom_volatility_trailing",
        "rule": (
            "28-day market top-third plus positive 56-day trend; BTC long; "
            "ATR trailing exit, disaster stop, and maximum hold"
        ),
        "selection": (
            "select parameters only on 2020-2023 by positive-year count, "
            "worst year, PF, then net return; freeze 2024-2026"
        ),
        "stress_one_way_cost": STRESS_ONE_WAY_COST,
        "selected_variant": selected_name,
        "selected_report": selected_report,
        "execution_delay_oos": delay_oos,
        "risk_sweep_full_period": risk_sweep,
        "accepted": accepted,
        "decision": (
            "eligible_for_realtime_shadow"
            if accepted
            else "rejected_not_stable_positive_expectancy"
        ),
        "all_variants": reports,
    }
    selected_trades.to_parquet(
        args.output / "selected_trades.parquet", index=False, compression="zstd"
    )
    (args.output / "report.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {key: value for key, value in output.items() if key != "all_variants"},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
