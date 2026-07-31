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

from scripts.benchmark_s0_donchian_ensemble import (  # noqa: E402
    DEFAULT_DATA,
    ONE_WAY_COST,
    daily_symbol_frame,
    donchian_positions,
    return_metrics,
)
from scripts.benchmark_s0_donchian_long_short import (  # noqa: E402
    extract_trades,
    long_short_positions,
)
from scripts.benchmark_s0_xmom_point_in_time import load_manifest  # noqa: E402


DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_major_trend"
LOOKBACK_SETS = {
    "fast": (5, 10, 20, 30),
    "full": (5, 10, 20, 30, 60, 90, 150, 250, 360),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark full-notional BTC/ETH Donchian trend baselines."
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def strategy_daily(
    frame: pd.DataFrame,
    lookbacks: tuple[int, ...],
    long_short: bool,
) -> pd.DataFrame:
    positions: list[pd.Series] = []
    for lookback in lookbacks:
        upper = frame.close.rolling(lookback, min_periods=lookback).max()
        lower = frame.close.rolling(lookback, min_periods=lookback).min()
        midpoint = (upper + lower) / 2.0
        if long_short:
            position = long_short_positions(
                frame.close, upper, lower, midpoint
            )
        else:
            position = donchian_positions(frame.close, upper, midpoint)
        positions.append(position)
    ensemble = pd.concat(positions, axis=1).mean(axis=1)
    if long_short:
        direction = pd.Series(
            np.where(ensemble.ge(0.5), 1, np.where(ensemble.le(-0.5), -1, 0)),
            index=frame.index,
        )
    else:
        direction = ensemble.ge(0.5).astype(int)
    turnover = direction.diff().abs().fillna(direction.abs())
    gross = direction * frame.next_return.fillna(0.0)
    return pd.DataFrame(
        {
            "day": frame.day,
            "symbol": np.where(direction.ne(0), frame.symbol, None),
            "direction": direction,
            "gross_return": gross,
            "turnover": turnover,
            "net_return": gross - turnover * ONE_WAY_COST,
        }
    )


def window_report(
    daily: pd.DataFrame,
    trades: pd.DataFrame,
    start: str,
    end: str,
) -> dict[str, Any]:
    daily_mask = daily.day.ge(start) & daily.day.lt(end)
    trade_mask = trades.entry_day.ge(start) & trades.entry_day.lt(end)
    scoped = trades.loc[trade_mask]
    winners = scoped.net_return.clip(lower=0).sum() if not scoped.empty else 0
    losers = -scoped.net_return.clip(upper=0).sum() if not scoped.empty else 0
    return {
        "daily": return_metrics(daily.loc[daily_mask]),
        "trades": {
            "trades": int(len(scoped)),
            "win_rate_pct": round(
                float(scoped.net_return.gt(0).mean() * 100), 6
            )
            if not scoped.empty
            else 0.0,
            "profit_factor": round(float(winners / losers), 6)
            if losers
            else (999.0 if winners else 0.0),
        },
    }


def evaluate(daily: pd.DataFrame) -> dict[str, Any]:
    trades = extract_trades(daily)
    return {
        "development_2024h2": window_report(
            daily, trades, "2024-07-01", "2025-01-01"
        ),
        "validation_2025h1": window_report(
            daily, trades, "2025-01-01", "2025-07-01"
        ),
        "test_2025h2": window_report(
            daily, trades, "2025-07-01", "2026-01-01"
        ),
    }


def main() -> None:
    args = parse_args()
    starts, manifest = load_manifest(args.data)
    reports: dict[str, Any] = {}
    outputs: dict[str, pd.DataFrame] = {}
    for symbol in ("BTCUSDT", "ETHUSDT"):
        path = args.data / "parquet" / f"{symbol}.parquet"
        frame = daily_symbol_frame(path, starts[symbol])
        for horizon_name, lookbacks in LOOKBACK_SETS.items():
            for long_short in (False, True):
                direction_name = "long_short" if long_short else "long_only"
                name = f"{symbol}_{horizon_name}_{direction_name}"
                daily = strategy_daily(frame, lookbacks, long_short)
                reports[name] = {
                    "symbol": symbol,
                    "lookbacks": list(lookbacks),
                    "direction_model": direction_name,
                    "minimum_vote": 0.5,
                    "windows": evaluate(daily),
                }
                outputs[name] = daily
    qualified = [
        name
        for name, report in reports.items()
        if all(
            report["windows"][window]["daily"].get("total_return_pct", 0) > 0
            and report["windows"][window]["trades"].get("profit_factor", 0)
            > 1.0
            for window in (
                "development_2024h2",
                "validation_2025h1",
                "test_2025h2",
            )
        )
    ]
    result = {
        "experiment": "s0_major_asset_time_series_trend",
        "source": manifest.get("source"),
        "checksum_verified": manifest.get("checksum_verified"),
        "full_notional_without_leverage": True,
        "one_way_cost_pct": ONE_WAY_COST * 100,
        "candidates": reports,
        "qualified_candidates": qualified,
        "decision": (
            "proceed_to_2026_and_minute_validation"
            if qualified
            else "research_only_not_eligible"
        ),
    }
    args.output.mkdir(parents=True, exist_ok=True)
    for name, daily in outputs.items():
        daily.to_parquet(args.output / f"{name}.parquet", index=False)
    (args.output / "report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
