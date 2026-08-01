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

from scripts.benchmark_s0_adaptive_30d_momentum import DEFAULT_DATA
from scripts.benchmark_s0_donchian_ensemble import daily_symbol_frame
from scripts.benchmark_s0_major_trend import LOOKBACK_SETS, strategy_daily
from scripts.benchmark_s0_xmom_point_in_time import load_manifest


DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_major_trend_cross_year"
COSTS = (0.0006, 0.0012)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit fixed BTC/ETH trend baselines across 2021-2026."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def load_symbol(symbol: str) -> pd.DataFrame:
    starts: dict[str, int] = {}
    for base in DEFAULT_DATA:
        partition_starts, _ = load_manifest(base)
        starts.update(partition_starts)
    parts = [
        daily_symbol_frame(base / "parquet" / f"{symbol}.parquet", starts[symbol])
        for base in DEFAULT_DATA
        if (base / "parquet" / f"{symbol}.parquet").exists()
    ]
    frame = (
        pd.concat(parts)
        .sort_values("day")
        .drop_duplicates("day", keep="last")
        .reset_index(drop=True)
    )
    frame["return"] = frame.close.pct_change()
    frame["next_return"] = frame.close.shift(-1) / frame.close - 1.0
    return frame


def extract_trades(daily: pd.DataFrame, one_way_cost: float) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    current_key: tuple[str, int] | None = None
    entry_day: Any = None
    returns: list[float] = []
    for row in daily.itertuples(index=False):
        key = (
            (str(row.symbol), int(row.direction))
            if pd.notna(row.symbol) and int(row.direction) != 0
            else None
        )
        if key != current_key:
            if current_key is not None:
                gross = float(np.prod(np.asarray(returns) + 1.0) - 1.0)
                records.append(
                    {
                        "entry_day": entry_day,
                        "exit_day": row.day,
                        "net_return": gross - 2.0 * one_way_cost,
                    }
                )
            current_key = key
            entry_day = row.day if key else None
            returns = []
        if current_key is not None:
            returns.append(float(row.gross_return))
    if current_key is not None:
        gross = float(np.prod(np.asarray(returns) + 1.0) - 1.0)
        records.append(
            {
                "entry_day": entry_day,
                "exit_day": daily.iloc[-1].day,
                "net_return": gross - 2.0 * one_way_cost,
            }
        )
    return pd.DataFrame(
        records, columns=["entry_day", "exit_day", "net_return"]
    )


def metrics(daily: pd.DataFrame, one_way_cost: float) -> dict[str, Any]:
    if daily.empty:
        return {"trades": 0, "net_pct": 0.0, "profit_factor": 0.0}
    net = daily.gross_return - daily.turnover * one_way_cost
    equity = (1.0 + net).cumprod()
    drawdown = equity / equity.cummax() - 1.0
    trades = extract_trades(daily, one_way_cost)
    wins = float(trades.net_return.clip(lower=0).sum())
    losses = float(-trades.net_return.clip(upper=0).sum())
    return {
        "trades": int(len(trades)),
        "win_rate_pct": (
            round(float(trades.net_return.gt(0).mean() * 100), 4)
            if not trades.empty
            else 0.0
        ),
        "profit_factor": (
            round(wins / losses, 4)
            if losses
            else (999.0 if wins else 0.0)
        ),
        "net_pct": round(float((equity.iloc[-1] - 1.0) * 100), 4),
        "max_drawdown_pct": round(float(drawdown.min() * 100), 4),
    }


def evaluate(name: str, daily: pd.DataFrame, cost: float) -> dict[str, Any]:
    annual: list[dict[str, Any]] = []
    for year in range(2021, 2027):
        scoped = daily.loc[
            daily.day.ge(f"{year}-01-01") & daily.day.lt(f"{year + 1}-01-01")
        ]
        if len(scoped) > 30:
            annual.append({"year": year, **metrics(scoped, cost)})
    return {
        "name": name,
        "one_way_cost_pct": cost * 100,
        "all": metrics(daily, cost),
        "annual": annual,
        "positive_years": sum(
            row["net_pct"] > 0 and row["profit_factor"] > 1 for row in annual
        ),
        "worst_year_net_pct": min(row["net_pct"] for row in annual),
        "worst_year_profit_factor": min(
            row["profit_factor"] for row in annual
        ),
    }


def sma200_daily(frame: pd.DataFrame) -> pd.DataFrame:
    signal = frame.close.gt(frame.close.rolling(200, min_periods=200).mean()).astype(
        int
    )
    return pd.DataFrame(
        {
            "day": frame.day,
            "symbol": np.where(signal.ne(0), frame.symbol, None),
            "direction": signal,
            "gross_return": signal * frame.next_return.fillna(0.0),
            "turnover": signal.diff().abs().fillna(signal),
        }
    )


def main() -> None:
    args = parse_args()
    frames = {symbol: load_symbol(symbol) for symbol in ("BTCUSDT", "ETHUSDT")}
    candidates: dict[str, pd.DataFrame] = {}
    for symbol, frame in frames.items():
        for horizon, lookbacks in LOOKBACK_SETS.items():
            for long_short in (False, True):
                side = "long_short" if long_short else "long_only"
                candidates[f"{symbol}_{horizon}_{side}"] = strategy_daily(
                    frame, lookbacks, long_short
                )
        candidates[f"{symbol}_sma200_long_cash"] = sma200_daily(frame)
    reports = [
        evaluate(name, daily, cost)
        for name, daily in candidates.items()
        for cost in COSTS
    ]
    qualified = [
        row["name"]
        for row in reports
        if row["one_way_cost_pct"] == COSTS[-1] * 100
        and row["positive_years"] == len(row["annual"])
        and row["all"]["max_drawdown_pct"] > -30
    ]
    result = {
        "experiment": "s0_major_trend_cross_year_audit",
        "period": "2021-2026",
        "costs_one_way": list(COSTS),
        "candidates": reports,
        "qualified_candidates": qualified,
        "decision": (
            "eligible_for_minute_validation"
            if qualified
            else "research_only_not_eligible"
        ),
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
