from __future__ import annotations

import argparse
import json
from itertools import product
from pathlib import Path
from typing import Any

import pandas as pd

from scripts.benchmark_s0_market_tsmom_28d import (
    BASE_ONE_WAY_COST,
    DEFAULT_DATA,
    STRESS_ONE_WAY_COST,
    btc_proxy_trades,
    build_daily_panel,
    market_state,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_market_tsmom_protection"
TAKE_PROFITS = (0.015, 0.020, 0.025, 0.030)
STOP_LOSSES = (0.040, 0.050, 0.060)
MAX_EQUITY_RISK = 0.30
MAX_LEVERAGE = 10.0


def load_hourly_btc(data_dirs: tuple[Path, ...]) -> pd.DataFrame:
    parts = []
    for root in data_dirs:
        path = root / "parquet" / "BTCUSDT.parquet"
        if path.exists():
            parts.append(
                pd.read_parquet(
                    path, columns=["open_time", "open", "high", "low", "close"]
                )
            )
    if not parts:
        raise ValueError("No BTCUSDT point-in-time hourly archives found")
    frame = (
        pd.concat(parts, ignore_index=True)
        .drop_duplicates("open_time", keep="last")
        .sort_values("open_time")
        .reset_index(drop=True)
    )
    frame["time"] = pd.to_datetime(frame.open_time, unit="ms", utc=True)
    return frame


def simulate_protection(
    signals: pd.DataFrame,
    hourly: pd.DataFrame,
    take_profit_pct: float | None,
    stop_loss_pct: float,
    one_way_cost: float,
) -> pd.DataFrame:
    indexed = hourly.set_index("time", drop=False)
    rows: list[dict[str, Any]] = []
    for signal in signals.itertuples(index=False):
        entry_time = pd.Timestamp(signal.entry_day)
        scheduled_exit = pd.Timestamp(signal.exit_day)
        if entry_time not in indexed.index or scheduled_exit not in indexed.index:
            continue
        entry_price = float(indexed.loc[entry_time, "open"])
        stop_price = entry_price * (1.0 - stop_loss_pct)
        take_profit_price = (
            entry_price * (1.0 + take_profit_pct)
            if take_profit_pct is not None
            else None
        )
        path = indexed.loc[
            (indexed.index >= entry_time) & (indexed.index < scheduled_exit)
        ]
        exit_time = scheduled_exit
        exit_price = float(indexed.loc[scheduled_exit, "open"])
        reason = "time_exit"
        for bar in path.itertuples(index=False):
            bar_open = float(bar.open)
            bar_low = float(bar.low)
            bar_high = float(bar.high)
            if bar_open <= stop_price:
                exit_time, exit_price, reason = bar.time, bar_open, "stop_gap"
                break
            if take_profit_price is not None and bar_open >= take_profit_price:
                exit_time, exit_price, reason = bar.time, bar_open, "take_profit_gap"
                break
            stop_hit = bar_low <= stop_price
            take_profit_hit = (
                take_profit_price is not None and bar_high >= take_profit_price
            )
            if stop_hit:
                exit_time, exit_price, reason = bar.time, stop_price, "stop"
                break
            if take_profit_hit and take_profit_price is not None:
                exit_time, exit_price, reason = (
                    bar.time,
                    take_profit_price,
                    "take_profit",
                )
                break
        gross_return = exit_price / entry_price - 1.0
        net_return = gross_return - 2.0 * float(one_way_cost)
        rows.append(
            {
                "signal_day": signal.day,
                "entry_day": signal.entry_day,
                "scheduled_exit_day": signal.exit_day,
                "exit_time": exit_time,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "reason": reason,
                "gross_return": gross_return,
                "net_return": net_return,
            }
        )
    return pd.DataFrame(rows)


def metrics(trades: pd.DataFrame, leverage: float) -> dict[str, float | int]:
    if trades.empty:
        return {
            "trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "net_return": 0.0,
            "max_drawdown": 0.0,
        }
    net = trades.net_return.astype(float)
    wins = float(net.clip(lower=0).sum())
    losses = float(-net.clip(upper=0).sum())
    equity_returns = net * float(leverage)
    equity = (1.0 + equity_returns).cumprod()
    drawdown = equity / equity.cummax() - 1.0
    return {
        "trades": int(len(trades)),
        "win_rate": round(float(net.gt(0).mean() * 100.0), 4),
        "profit_factor": round(wins / losses, 4)
        if losses
        else (999.0 if wins else 0.0),
        "net_return": round(float(equity.iloc[-1] - 1.0), 6),
        "max_drawdown": round(float(drawdown.min()), 6),
        "take_profit_exits": int(trades.reason.str.startswith("take_profit").sum()),
        "stop_exits": int(trades.reason.str.startswith("stop").sum()),
        "time_exits": int(trades.reason.eq("time_exit").sum()),
    }


def report(trades: pd.DataFrame, leverage: float) -> dict[str, Any]:
    if trades.empty:
        return {"overall": metrics(trades, leverage), "annual": {}}
    years = pd.to_datetime(trades.entry_day, utc=True).dt.year
    return {
        "overall": metrics(trades, leverage),
        "annual": {
            str(int(year)): metrics(group, leverage)
            for year, group in trades.assign(year=years).groupby("year", sort=True)
        },
    }


def training_score(report_: dict[str, Any]) -> tuple[int, float, float, float]:
    overall = report_["overall"]
    annual_returns = [
        float(year["net_return"]) for year in report_["annual"].values()
    ]
    eligible = int(
        overall["trades"] >= 50
        and overall["win_rate"] >= 60.0
        and overall["profit_factor"] > 1.10
        and overall["max_drawdown"] > -0.80
    )
    return (
        eligible,
        min(annual_returns, default=-999.0),
        float(overall["profit_factor"]),
        float(overall["win_rate"]),
    )


def qualifies_oos(report_: dict[str, Any]) -> bool:
    required = {"2024", "2025", "2026"}
    annual = report_["annual"]
    overall = report_["overall"]
    return bool(
        required.issubset(annual)
        and overall["win_rate"] >= 60.0
        and overall["profit_factor"] > 1.10
        and overall["max_drawdown"] > -0.80
        and all(annual[year]["net_return"] > 0 for year in required)
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit fixed protection for the 28-day BTC market timing candidate."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    panel = build_daily_panel(DEFAULT_DATA)
    signals = btc_proxy_trades(panel, market_state(panel))
    hourly = load_hourly_btc(DEFAULT_DATA)
    candidates: dict[str, Any] = {}
    training_reports: dict[str, dict[str, Any]] = {}
    for take_profit_pct, stop_loss_pct in product(TAKE_PROFITS, STOP_LOSSES):
        label = f"tp{take_profit_pct:.3f}_sl{stop_loss_pct:.3f}"
        leverage = min(
            MAX_LEVERAGE,
            MAX_EQUITY_RISK / (stop_loss_pct + 2.0 * STRESS_ONE_WAY_COST),
        )
        stress_trades = simulate_protection(
            signals,
            hourly,
            take_profit_pct,
            stop_loss_pct,
            STRESS_ONE_WAY_COST,
        )
        base_trades = simulate_protection(
            signals,
            hourly,
            take_profit_pct,
            stop_loss_pct,
            BASE_ONE_WAY_COST,
        )
        training = stress_trades.loc[
            pd.to_datetime(stress_trades.entry_day, utc=True).dt.year.le(2023)
        ]
        oos = stress_trades.loc[
            pd.to_datetime(stress_trades.entry_day, utc=True).dt.year.ge(2024)
        ]
        training_report = report(training, leverage)
        training_reports[label] = training_report
        candidates[label] = {
            "take_profit_pct": take_profit_pct,
            "stop_loss_pct": stop_loss_pct,
            "max_safe_leverage": round(leverage, 6),
            "training_stress": training_report,
            "oos_stress": report(oos, leverage),
            "full_base": report(base_trades, leverage),
            "full_stress": report(stress_trades, leverage),
        }
    selected = max(
        training_reports, key=lambda label: training_score(training_reports[label])
    )
    accepted = qualifies_oos(candidates[selected]["oos_stress"])
    selected_config = candidates[selected]
    selected_stress = simulate_protection(
        signals,
        hourly,
        selected_config["take_profit_pct"],
        selected_config["stop_loss_pct"],
        STRESS_ONE_WAY_COST,
    )
    selected_stress.to_parquet(args.output / "selected_stress_trades.parquet", index=False)
    result = {
        "experiment": "s0_market_tsmom_fixed_protection",
        "selection": "choose protection only on 2020-2023 stress results; freeze 2024-2026",
        "intrabar_rule": "if stop and take-profit touch in same hour, stop executes first",
        "max_equity_risk": MAX_EQUITY_RISK,
        "selected": selected,
        "selected_result": selected_config,
        "accepted": accepted,
        "decision": "eligible_for_minute_validation" if accepted else "rejected_not_stable_positive_expectancy",
        "candidates": candidates,
    }
    (args.output / "report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({key: value for key, value in result.items() if key != "candidates"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
