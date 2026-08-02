from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from scripts.benchmark_s0_market_tsmom_28d import (
    DEFAULT_DATA,
    HISTORY_DAYS,
    HOLD_DAYS,
    STRESS_ONE_WAY_COST,
    build_daily_panel,
    evaluate,
    market_state,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_market_tsmom_symmetric"
SLOW_LOOKBACK = 56


def directional_state(state: pd.DataFrame) -> pd.DataFrame:
    frame = state.copy()
    frame["historical_bottom_third"] = (
        frame.momentum_28d.expanding(min_periods=HISTORY_DAYS)
        .quantile(1.0 / 3.0)
        .shift(1)
    )
    frame["slow_momentum"] = frame.market_index.pct_change(SLOW_LOOKBACK)
    frame["direction"] = 0
    frame.loc[
        frame.universe_size.ge(20)
        & frame.momentum_28d.gt(frame.historical_top_third)
        & frame.slow_momentum.gt(0.0),
        "direction",
    ] = 1
    frame.loc[
        frame.universe_size.ge(20)
        & frame.momentum_28d.lt(frame.historical_bottom_third)
        & frame.slow_momentum.lt(0.0),
        "direction",
    ] = -1
    return frame


def directional_proxy_trades(
    panel: pd.DataFrame, state: pd.DataFrame, symbol: str = "BTCUSDT"
) -> pd.DataFrame:
    proxy = (
        panel.loc[panel.symbol.eq(symbol), ["day", "open"]]
        .drop_duplicates("day", keep="last")
        .sort_values("day")
        .reset_index(drop=True)
    )
    frame = state.merge(proxy, on="day", how="inner").sort_values("day").reset_index(
        drop=True
    )
    frame["entry_day"] = frame.day.shift(-1)
    frame["exit_day"] = frame.day.shift(-1 - HOLD_DAYS)
    frame["asset_return"] = frame.open.shift(-1 - HOLD_DAYS) / frame.open.shift(-1) - 1.0
    frame["gross_return"] = frame.asset_return * frame.direction
    candidates = frame.loc[
        frame.direction.ne(0) & frame.gross_return.notna(),
        ["day", "entry_day", "exit_day", "direction", "gross_return"],
    ].copy()
    selected: list[int] = []
    available_day = pd.Timestamp.min.tz_localize("UTC")
    for row in candidates.itertuples():
        if row.entry_day >= available_day:
            selected.append(int(row.Index))
            available_day = row.exit_day
    return candidates.loc[selected].reset_index(drop=True)


def split_report(trades: pd.DataFrame) -> dict[str, Any]:
    years = pd.to_datetime(trades.entry_day, utc=True).dt.year
    training = trades.loc[years.le(2023)]
    oos = trades.loc[years.ge(2024)]
    return {
        "training_stress": evaluate(training, STRESS_ONE_WAY_COST),
        "oos_stress": evaluate(oos, STRESS_ONE_WAY_COST),
        "full_stress": evaluate(trades, STRESS_ONE_WAY_COST),
        "long_stress": evaluate(trades.loc[trades.direction.eq(1)], STRESS_ONE_WAY_COST),
        "short_stress": evaluate(trades.loc[trades.direction.eq(-1)], STRESS_ONE_WAY_COST),
    }


def qualifies(result: dict[str, Any]) -> bool:
    annual = result["full_stress"]["annual"]
    overall = result["full_stress"]["overall"]
    required = {str(year) for year in range(2021, 2027)}
    return bool(
        required.issubset(annual)
        and overall["profit_factor"] > 1.10
        and overall["max_drawdown"] > -0.60
        and all(
            annual[year]["net_return"] > 0
            and annual[year]["profit_factor"] > 1.0
            for year in required
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit a symmetric long/short market trend strategy."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    panel = build_daily_panel(DEFAULT_DATA)
    state = directional_state(market_state(panel))
    trades = directional_proxy_trades(panel, state)
    result = split_report(trades)
    accepted = qualifies(result)
    output = {
        "experiment": "s0_market_tsmom_symmetric_long_short",
        "rule": "long above historical top-third with positive 56-day trend; short below bottom-third with negative 56-day trend; hold five days",
        "result": result,
        "accepted": accepted,
        "decision": "eligible_for_hourly_path_validation"
        if accepted
        else "rejected_not_stable_positive_expectancy",
    }
    state.to_parquet(args.output / "directional_state.parquet", index=False)
    trades.to_parquet(args.output / "trades.parquet", index=False)
    (args.output / "report.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
