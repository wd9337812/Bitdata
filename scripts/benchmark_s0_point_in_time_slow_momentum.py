from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_s0_xmom_regime_candidates import (  # noqa: E402
    BASE_COST,
    STRESS_COST,
    block_bootstrap,
    cost_adjusted,
    development_folds,
    scope,
)
from scripts.benchmark_s0_cross_sectional_momentum import (  # noqa: E402
    Profile,
    simulate,
    summarize,
)
from scripts.benchmark_s0_xmom_point_in_time import (  # noqa: E402
    DEFAULT_DATA,
    build_panel,
    load_manifest,
)


DEFAULT_PANEL = (
    ROOT
    / "data"
    / "research"
    / "s0_xmom_point_in_time_july_audit"
    / "point_in_time_panel.parquet"
)
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_point_in_time_slow_momentum"


@dataclass(frozen=True)
class SlowMomentumCandidate:
    name: str
    formation_hours: int
    cadence_hours: int
    hold_hours: int
    stop_atr: float
    reward_r: float
    blend_shorter_horizon: bool = False
    minimum_liquidity_24h: float = 20_000_000


CANDIDATES = (
    SlowMomentumCandidate("momentum_7d_hold_1d", 168, 6, 24, 2.0, 2.0),
    SlowMomentumCandidate("momentum_14d_hold_3d", 336, 12, 72, 2.5, 2.5),
    SlowMomentumCandidate("momentum_30d_hold_7d", 720, 24, 168, 3.0, 3.0),
    SlowMomentumCandidate(
        "momentum_30d_7d_blend_hold_7d",
        720,
        24,
        168,
        3.0,
        3.0,
        True,
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark lower-turnover 7-30 day point-in-time crypto momentum."
        )
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--panel", type=Path, default=DEFAULT_PANEL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--minimum-age-days", type=int, default=45)
    return parser.parse_args()


def add_slow_returns(panel: pd.DataFrame) -> pd.DataFrame:
    result = panel.sort_values(["symbol", "available_ms"]).copy()
    grouped = result.groupby("symbol", sort=False)
    for hours in (168, 336, 720):
        result[f"ret_{hours}h"] = grouped.close.pct_change(hours)
    return result.sort_values(["available_ms", "symbol"]).reset_index(drop=True)


def slow_momentum_signals(
    panel: pd.DataFrame,
    candidate: SlowMomentumCandidate,
    minimum_age_days: int,
) -> pd.DataFrame:
    return_column = f"ret_{candidate.formation_hours}h"
    eligible = panel.loc[
        ~panel.symbol.isin({"BTCUSDT", "ETHUSDT"})
        & panel.symbol_age_days.ge(minimum_age_days)
        & panel.liquidity_24h.ge(candidate.minimum_liquidity_24h)
        & panel[return_column].notna()
        & ((panel.available_ms // 3_600_000) % candidate.cadence_hours).eq(0)
    ].copy()
    eligible["universe_size"] = eligible.groupby(
        "available_ms", sort=False
    ).symbol.transform("nunique")
    breadth = eligible.groupby("available_ms", sort=False)[return_column].median()
    btc = (
        panel.loc[panel.symbol.eq("BTCUSDT")]
        .set_index("available_ms")[return_column]
    )
    eligible["market_breadth"] = eligible.available_ms.map(breadth)
    eligible["btc_return"] = eligible.available_ms.map(btc)
    eligible["market_direction"] = np.select(
        [
            eligible.market_breadth.gt(0) & eligible.btc_return.gt(0),
            eligible.market_breadth.lt(0) & eligible.btc_return.lt(0),
        ],
        ["LONG", "SHORT"],
        default="MIXED",
    )
    eligible = eligible.loc[
        eligible.universe_size.ge(60)
        & eligible.market_direction.ne("MIXED")
    ].copy()
    eligible["formation_rank"] = eligible.groupby(
        "available_ms", sort=False
    )[return_column].rank(pct=True)
    if candidate.blend_shorter_horizon:
        eligible["short_rank"] = eligible.groupby(
            "available_ms", sort=False
        ).ret_168h.rank(pct=True)
        eligible["momentum_score"] = (
            eligible.formation_rank + eligible.short_rank
        ) / 2
    else:
        eligible["momentum_score"] = eligible.formation_rank
    eligible["strength"] = np.where(
        eligible.market_direction.eq("LONG"),
        eligible.momentum_score,
        1.0 - eligible.momentum_score,
    )
    selected = (
        eligible.sort_values(
            ["available_ms", "strength", "liquidity_24h"],
            ascending=[True, False, False],
        )
        .groupby("available_ms", sort=False)
        .head(1)
        .copy()
    )
    selected["direction"] = selected.market_direction
    return selected.sort_values("available_ms").reset_index(drop=True)


def profile_for(candidate: SlowMomentumCandidate) -> Profile:
    return Profile(
        candidate.name,
        (f"ret_{candidate.formation_hours}h",),
        0.05,
        candidate.stop_atr,
        candidate.reward_r,
        candidate.hold_hours,
    )


def evaluate_candidate(
    panel: pd.DataFrame,
    candidate: SlowMomentumCandidate,
    minimum_age_days: int,
) -> tuple[dict[str, Any], pd.DataFrame]:
    signals = slow_momentum_signals(panel, candidate, minimum_age_days)
    trades = simulate(
        signals,
        panel,
        profile_for(candidate),
        cost_pct=BASE_COST,
    )
    stressed = cost_adjusted(trades, STRESS_COST)
    folds = development_folds(trades)
    stress_folds = development_folds(stressed)
    fold_reports = {
        name: {
            "base": summarize(frame),
            "stress": summarize(stress_folds[name]),
        }
        for name, frame in folds.items()
    }
    development = scope(trades, "2026-02-01", "2026-04-01")
    development_stress = scope(stressed, "2026-02-01", "2026-04-01")
    dev_base = summarize(development)
    dev_stress = summarize(development_stress)
    minimum_fold_trades = min(
        item["base"].get("trades", 0)
        for item in fold_reports.values()
    )
    qualified = bool(
        dev_base.get("trades", 0) >= 12
        and minimum_fold_trades >= 8
        and sum(
            item["base"].get("net_pct_points", 0) > 0
            for item in fold_reports.values()
        )
        >= 2
        and sum(
            item["stress"].get("net_pct_points", 0) > 0
            for item in fold_reports.values()
        )
        >= 2
        and dev_base.get("profit_factor", 0) >= 1.10
        and dev_stress.get("profit_factor", 0) >= 1.02
    )
    return {
        "candidate": asdict(candidate),
        "signals": int(len(signals)),
        "development": {
            "base": dev_base,
            "stress": dev_stress,
            "minimum_fold_trades": minimum_fold_trades,
            "folds": fold_reports,
        },
        "development_qualified": qualified,
    }, trades


def frozen_report(trades: pd.DataFrame) -> dict[str, Any]:
    stressed = cost_adjusted(trades, STRESS_COST)
    report: dict[str, Any] = {}
    for name, start, end in (
        ("validation_apr_may", "2026-04-01", "2026-06-01"),
        ("test_june", "2026-06-01", "2026-07-01"),
        ("final_july", "2026-07-01", "2026-08-01"),
    ):
        base = scope(trades, start, end)
        stress = scope(stressed, start, end)
        report[name] = {
            "base": summarize(base),
            "stress": summarize(stress),
            "bootstrap_base": block_bootstrap(base),
            "bootstrap_stress": block_bootstrap(stress),
        }
    return report


def main() -> None:
    args = parse_args()
    starts, manifest = load_manifest(args.data)
    panel = (
        pd.read_parquet(args.panel)
        if args.panel.exists()
        else build_panel(args.data, starts)
    )
    panel = add_slow_returns(panel)
    reports: dict[str, dict[str, Any]] = {}
    trades: dict[str, pd.DataFrame] = {}
    for candidate in CANDIDATES:
        report, candidate_trades = evaluate_candidate(
            panel,
            candidate,
            args.minimum_age_days,
        )
        reports[candidate.name] = report
        trades[candidate.name] = candidate_trades
    eligible = [
        (name, report)
        for name, report in reports.items()
        if report["development_qualified"]
    ]
    selected = max(
        eligible,
        key=lambda item: (
            item[1]["development"]["stress"].get("profit_factor", 0),
            item[1]["development"]["stress"].get("net_pct_points", 0),
        ),
        default=None,
    )
    frozen = frozen_report(trades[selected[0]]) if selected else None
    qualified = bool(
        frozen
        and all(
            frozen[name]["base"].get("trades", 0) >= 6
            and frozen[name]["base"].get("profit_factor", 0) > 1.05
            and frozen[name]["stress"].get("profit_factor", 0) > 1.0
            for name in frozen
        )
    )
    result = {
        "experiment": "s0_point_in_time_slow_momentum",
        "source": manifest.get("source"),
        "symbols": int(panel.symbol.nunique()),
        "cost_pct": BASE_COST,
        "stress_cost_pct": STRESS_COST,
        "selection": "development_only_2026_02_to_2026_03",
        "candidates": reports,
        "selected_candidate": selected[0] if selected else None,
        "frozen": frozen,
        "qualified_for_shadow": qualified,
        "decision": (
            "qualified_for_research_shadow"
            if qualified
            else "research_only_not_eligible"
        ),
    }
    args.output.mkdir(parents=True, exist_ok=True)
    if selected:
        selected_candidate = next(
            item for item in CANDIDATES if item.name == selected[0]
        )
        selected_signals = slow_momentum_signals(
            panel,
            selected_candidate,
            args.minimum_age_days,
        )
        selected_signals.to_parquet(
            args.output / "selected_signals.parquet",
            index=False,
        )
        trades[selected[0]].to_parquet(
            args.output / "selected_trades_1h.parquet",
            index=False,
        )
    (args.output / "report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
