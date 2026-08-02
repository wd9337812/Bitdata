from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_s0_market_tsmom_28d import (
    DEFAULT_DATA,
    TOP_LIQUID_SYMBOLS,
    build_daily_panel,
    market_state,
)
from scripts.benchmark_s0_market_tsmom_consensus import consensus_state
from scripts.benchmark_s0_market_tsmom_trailing import (
    SLOW_LOOKBACK,
    Variant,
    btc_daily,
    metrics,
    simulate,
)


DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_market_tsmom_confirmations"
TRAIN_END_YEAR = 2023
TRAILING = Variant(10, 3.0, 0.15, 20)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit predeclared trend, breadth, and volume confirmations."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def confirmation_features(panel: pd.DataFrame) -> pd.DataFrame:
    eligible = panel.dropna(subset=["return", "median_volume_30d"]).copy()
    eligible["liquidity_rank"] = eligible.groupby("day").median_volume_30d.rank(
        method="first", ascending=False
    )
    liquid = eligible.loc[eligible.liquidity_rank.le(TOP_LIQUID_SYMBOLS)].copy()
    liquid["momentum_28d"] = liquid.groupby("symbol").close.pct_change(
        28, fill_method=None
    )
    market = (
        liquid.groupby("day", as_index=False)
        .agg(
            breadth_positive_28d=("momentum_28d", lambda value: float(value.gt(0).mean())),
            market_quote_volume=("quote_volume", "sum"),
        )
        .sort_values("day")
    )
    market["volume_7_to_28"] = (
        market.market_quote_volume.rolling(7, min_periods=7).mean()
        / market.market_quote_volume.rolling(28, min_periods=28).mean()
    )
    btc = (
        panel.loc[panel.symbol.eq("BTCUSDT"), ["day", "close"]]
        .drop_duplicates("day", keep="last")
        .sort_values("day")
    )
    btc["btc_ma_200"] = btc.close.rolling(200, min_periods=200).mean()
    btc["btc_momentum_56d"] = btc.close.pct_change(56, fill_method=None)
    return market.merge(btc, on="day", how="left")


def predeclared_signals(state: pd.DataFrame, features: pd.DataFrame) -> dict[str, pd.DataFrame]:
    frame = state.merge(features, on="day", how="left")
    conditions = {
        "base": pd.Series(True, index=frame.index),
        "btc_above_ma200": frame.close.gt(frame.btc_ma_200),
        "btc_momentum56_positive": frame.btc_momentum_56d.gt(0),
        "breadth55": frame.breadth_positive_28d.ge(0.55),
        "breadth60": frame.breadth_positive_28d.ge(0.60),
        "volume7_above_28": frame.volume_7_to_28.ge(1.0),
        "ma200_and_volume": frame.close.gt(frame.btc_ma_200)
        & frame.volume_7_to_28.ge(1.0),
        "breadth55_and_volume": frame.breadth_positive_28d.ge(0.55)
        & frame.volume_7_to_28.ge(1.0),
    }
    return {
        name: frame.assign(signal=frame.signal.fillna(False) & condition.fillna(False))
        for name, condition in conditions.items()
    }


def split_report(trades: pd.DataFrame) -> dict[str, Any]:
    years = pd.to_datetime(trades.entry_day, utc=True).dt.year
    return {
        "training": metrics(
            trades.loc[years.le(TRAIN_END_YEAR)],
            stop_pct=TRAILING.disaster_stop_pct,
        ),
        "out_of_sample": metrics(
            trades.loc[years.gt(TRAIN_END_YEAR)],
            stop_pct=TRAILING.disaster_stop_pct,
        ),
        "annual": {
            str(int(year)): metrics(group, stop_pct=TRAILING.disaster_stop_pct)
            for year, group in trades.assign(year=years).groupby("year", sort=True)
        },
    }


def training_score(report: dict[str, Any]) -> tuple[int, float, float, int]:
    years = [report["annual"].get(str(year), {}) for year in range(2020, 2024)]
    positive_years = sum(float(item.get("net_return", 0)) > 0 for item in years)
    worst_year = min(float(item.get("net_return", -1)) for item in years)
    return (
        positive_years,
        worst_year,
        float(report["training"].get("profit_factor", 0)),
        int(report["training"].get("trades", 0)),
    )


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    panel = build_daily_panel(DEFAULT_DATA)
    base = consensus_state(market_state(panel), SLOW_LOOKBACK)
    btc = btc_daily(panel)
    signal_frames = predeclared_signals(base, confirmation_features(panel))
    reports = {
        name: split_report(simulate(btc, signal, TRAILING))
        for name, signal in signal_frames.items()
    }
    selected = max(reports, key=lambda name: training_score(reports[name]))
    selected_report = reports[selected]
    annual = selected_report["annual"]
    accepted = bool(
        selected_report["training"]["profit_factor"] > 1.10
        and selected_report["out_of_sample"]["profit_factor"] > 1.10
        and all(
            float(annual.get(str(year), {}).get("net_return", -1)) > 0
            for year in (2024, 2025, 2026)
        )
    )
    result = {
        "experiment": "s0_market_tsmom_predeclared_confirmations",
        "candidate_exit": TRAILING.name,
        "selection": (
            "Eight literature-motivated confirmations declared before evaluation; "
            "select on 2020-2023 positive years, worst year, PF, then sample count; "
            "freeze 2024-2026."
        ),
        "selected_confirmation": selected,
        "selected_report": selected_report,
        "all_confirmations": reports,
        "accepted": accepted,
        "decision": (
            "eligible_for_realtime_shadow"
            if accepted
            else "rejected_not_stable_positive_expectancy"
        ),
    }
    (args.output / "report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
