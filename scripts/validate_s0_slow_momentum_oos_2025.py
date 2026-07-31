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

from scripts.benchmark_s0_cross_sectional_momentum import (  # noqa: E402
    simulate,
    summarize,
)
from scripts.benchmark_s0_point_in_time_slow_momentum import (  # noqa: E402
    CANDIDATES,
    add_slow_returns,
    profile_for,
    slow_momentum_signals,
)
from scripts.benchmark_s0_xmom_point_in_time import (  # noqa: E402
    build_panel,
    load_manifest,
)


DEFAULT_DATA = (
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2025h2"
)
DEFAULT_OUTPUT = (
    ROOT / "data" / "research" / "s0_slow_momentum_oos_2025h2"
)
CANDIDATE_NAME = "momentum_7d_hold_1d"
BASE_COST_PCT = 0.12
STRESS_COST_PCT = 0.24


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the frozen 2026 slow-momentum candidate on untouched "
            "point-in-time 2025H2 data without selecting or tuning parameters."
        )
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--minimum-age-days", type=int, default=45)
    return parser.parse_args()


def cost_adjusted(trades: pd.DataFrame, cost_pct: float) -> pd.DataFrame:
    result = trades.copy()
    if not result.empty:
        result["cost_pct"] = cost_pct
        result["net_pct"] = result.gross_pct - cost_pct
    return result


def monthly_metrics(trades: pd.DataFrame) -> dict[str, dict[str, Any]]:
    if trades.empty:
        return {}
    result = trades.copy()
    result["month"] = pd.to_datetime(
        result.entry_ms,
        unit="ms",
        utc=True,
    ).dt.strftime("%Y-%m")
    return {
        str(month): summarize(frame)
        for month, frame in result.groupby("month", sort=True)
    }


def concentration_metrics(trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty:
        return {}
    contributions = trades.groupby("symbol").net_pct.sum().sort_values(
        ascending=False
    )
    positive_total = float(contributions.clip(lower=0).sum())
    top = contributions.head(5)
    return {
        "top_five_symbols": {
            str(symbol): round(float(value), 6)
            for symbol, value in top.items()
        },
        "top_five_share_of_positive_pct": round(
            float(top.clip(lower=0).sum() / positive_total * 100),
            4,
        )
        if positive_total > 0
        else 0.0,
        "net_without_best_symbol_pct_points": round(
            float(
                trades.loc[
                    trades.symbol.ne(str(contributions.index[0])), "net_pct"
                ].sum()
            ),
            6,
        ),
    }


def scenario_report(trades: pd.DataFrame) -> dict[str, Any]:
    return {
        "overall": summarize(trades),
        "monthly": monthly_metrics(trades),
        "concentration": concentration_metrics(trades),
    }


def qualifies(scenarios: dict[str, dict[str, Any]]) -> bool:
    base = scenarios["delay_0h_cost_0.12pct"]
    stress = scenarios["delay_0h_cost_0.24pct"]
    delayed = scenarios["delay_1h_cost_0.12pct"]
    months = base["monthly"]
    positive_months = sum(
        item.get("net_pct_points", 0) > 0 for item in months.values()
    )
    return bool(
        base["overall"].get("trades", 0) >= 30
        and len(months) >= 5
        and positive_months >= 4
        and base["overall"].get("profit_factor", 0) > 1.10
        and stress["overall"].get("profit_factor", 0) > 1.02
        and delayed["overall"].get("profit_factor", 0) > 1.0
        and base["concentration"].get(
            "net_without_best_symbol_pct_points", 0
        )
        > 0
    )


def main() -> None:
    args = parse_args()
    starts, manifest = load_manifest(args.data)
    if manifest.get("start_month") != "2025-07" or manifest.get(
        "end_month"
    ) != "2025-12":
        raise ValueError("Expected the pre-registered 2025-07 to 2025-12 window")
    panel = add_slow_returns(build_panel(args.data, starts))
    candidate = next(
        item for item in CANDIDATES if item.name == CANDIDATE_NAME
    )
    signals = slow_momentum_signals(
        panel,
        candidate,
        args.minimum_age_days,
    )
    scenarios: dict[str, dict[str, Any]] = {}
    trade_outputs: dict[str, pd.DataFrame] = {}
    for delay in (0, 1):
        raw = simulate(
            signals,
            panel,
            profile_for(candidate),
            cost_pct=BASE_COST_PCT,
            execution_delay_hours=delay,
        )
        for cost in (BASE_COST_PCT, STRESS_COST_PCT):
            name = f"delay_{delay}h_cost_{cost:.2f}pct"
            adjusted = cost_adjusted(raw, cost)
            scenarios[name] = scenario_report(adjusted)
            trade_outputs[name] = adjusted
    qualified = qualifies(scenarios)
    report = {
        "experiment": "s0_slow_momentum_frozen_oos_2025h2",
        "candidate": CANDIDATE_NAME,
        "selection_or_tuning_on_2025h2": False,
        "source": manifest.get("source"),
        "checksum_verified": manifest.get("checksum_verified"),
        "symbols": int(panel.symbol.nunique()),
        "signals": int(len(signals)),
        "scenarios": scenarios,
        "qualified_for_minute_validation": qualified,
        "decision": (
            "proceed_to_minute_validation"
            if qualified
            else "reject_frozen_candidate"
        ),
    }
    args.output.mkdir(parents=True, exist_ok=True)
    signals.to_parquet(args.output / "selected_signals.parquet", index=False)
    for name, trades in trade_outputs.items():
        trades.to_parquet(args.output / f"trades_{name}.parquet", index=False)
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
