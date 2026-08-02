from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = (
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2020_2023",
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2024_2025",
    ROOT / "data" / "research" / "binance_um_point_in_time_1h",
)
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_btc_12week_timing"
LOOKBACK_DAYS = 84
BASE_ONE_WAY_COST = 0.0006
STRESS_ONE_WAY_COST = 0.0012


def load_daily_btc(data_dirs: tuple[Path, ...]) -> pd.DataFrame:
    frames = []
    for root in data_dirs:
        path = root / "parquet" / "BTCUSDT.parquet"
        if path.exists():
            frames.append(
                pd.read_parquet(path, columns=["open_time", "open", "close"])
            )
    if not frames:
        raise ValueError("No BTCUSDT point-in-time hourly archives found")
    hourly = (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates("open_time", keep="last")
        .sort_values("open_time")
    )
    hourly["day"] = pd.to_datetime(hourly.open_time, unit="ms", utc=True).dt.floor(
        "D"
    )
    daily = (
        hourly.groupby("day", as_index=False)
        .agg(open=("open", "first"), close=("close", "last"))
        .sort_values("day")
        .reset_index(drop=True)
    )
    daily["asset_return"] = daily.close.pct_change(fill_method=None)
    daily["momentum_12week"] = daily.close.pct_change(
        LOOKBACK_DAYS, fill_method=None
    )
    return daily


def strategy_returns(
    daily: pd.DataFrame, one_way_cost: float, *, allow_short: bool
) -> pd.DataFrame:
    frame = daily.copy()
    signal = np.sign(frame.momentum_12week).where(
        frame.momentum_12week.notna(), 0.0
    )
    if not allow_short:
        signal = signal.clip(lower=0.0)
    frame["position"] = signal.shift(1).fillna(0.0)
    frame["turnover"] = frame.position.diff().abs().fillna(frame.position.abs())
    frame["gross_return"] = frame.position * frame.asset_return.fillna(0.0)
    frame["net_return"] = (
        frame.gross_return - frame.turnover * float(one_way_cost)
    )
    return frame


def episode_returns(frame: pd.DataFrame) -> pd.Series:
    active = frame.position.ne(0)
    starts = active & (~active.shift(1, fill_value=False) | frame.position.ne(frame.position.shift(1)))
    episode_id = starts.cumsum().where(active)
    return frame.loc[active].groupby(episode_id[active]).net_return.apply(
        lambda values: (1.0 + values).prod() - 1.0
    )


def metrics(frame: pd.DataFrame) -> dict[str, float | int]:
    net = frame.net_return.fillna(0.0)
    equity = (1.0 + net).cumprod()
    drawdown = equity / equity.cummax() - 1.0
    episodes = episode_returns(frame)
    wins = float(episodes.clip(lower=0).sum())
    losses = float(-episodes.clip(upper=0).sum())
    volatility = float(net.std(ddof=0))
    return {
        "days": int(len(frame)),
        "episodes": int(len(episodes)),
        "episode_win_rate": round(float(episodes.gt(0).mean() * 100.0), 4)
        if len(episodes)
        else 0.0,
        "episode_profit_factor": round(wins / losses, 4)
        if losses
        else (999.0 if wins else 0.0),
        "net_return": round(float(equity.iloc[-1] - 1.0), 6),
        "sharpe": round(float(net.mean() / volatility * np.sqrt(365.0)), 4)
        if volatility
        else 0.0,
        "max_drawdown": round(float(drawdown.min()), 6),
        "exposure_pct": round(float(frame.position.ne(0).mean() * 100.0), 4),
        "turnover": round(float(frame.turnover.sum()), 4),
    }


def evaluate(frame: pd.DataFrame) -> dict[str, Any]:
    years = pd.to_datetime(frame.day, utc=True).dt.year
    return {
        "overall": metrics(frame),
        "annual": {
            str(int(year)): metrics(group)
            for year, group in frame.assign(year=years).groupby("year", sort=True)
        },
    }


def qualifies(report: dict[str, Any]) -> bool:
    required = {str(year) for year in range(2021, 2027)}
    annual = report["annual"]
    overall = report["overall"]
    return bool(
        required.issubset(annual)
        and overall["episode_profit_factor"] > 1.10
        and overall["max_drawdown"] > -0.60
        and all(annual[year]["net_return"] > 0 for year in required)
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit a fixed 12-week slow BTC timing proxy."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    daily = load_daily_btc(DEFAULT_DATA)
    args.output.mkdir(parents=True, exist_ok=True)
    candidates: dict[str, Any] = {}
    accepted: list[str] = []
    for label, allow_short in (("long_cash", False), ("long_short", True)):
        reports = {}
        for cost_label, cost in (
            ("base", BASE_ONE_WAY_COST),
            ("stress", STRESS_ONE_WAY_COST),
        ):
            frame = strategy_returns(daily, cost, allow_short=allow_short)
            reports[cost_label] = evaluate(frame)
            frame.to_parquet(
                args.output / f"{label}_{cost_label}_daily.parquet", index=False
            )
        candidates[label] = reports
        if qualifies(reports["stress"]):
            accepted.append(label)
    result = {
        "experiment": "s0_btc_fixed_12week_slow_timing",
        "source_method": "Kang-Ryu 2026 slow Bitcoin momentum proxy",
        "rule": "at day close, use sign of trailing 84-day BTC return for next-day exposure",
        "lookback_days": LOOKBACK_DAYS,
        "base_one_way_cost": BASE_ONE_WAY_COST,
        "stress_one_way_cost": STRESS_ONE_WAY_COST,
        "candidates": candidates,
        "accepted": accepted,
        "decision": "eligible_for_intraday_path_validation" if accepted else "rejected_not_stable_positive_expectancy",
    }
    (args.output / "report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
