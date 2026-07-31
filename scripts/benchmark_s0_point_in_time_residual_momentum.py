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
DEFAULT_OUTPUT = (
    ROOT / "data" / "research" / "s0_point_in_time_residual_momentum"
)
PROFILE = Profile(
    "residual_momentum_24h",
    ("ret_24h",),
    0.05,
    1.2,
    1.8,
    12,
)


@dataclass(frozen=True)
class ResidualCandidate:
    name: str
    score: str
    dispersion_gate: str = "none"
    require_persistence: bool = False
    minimum_liquidity_24h: float = 20_000_000


CANDIDATES = (
    ResidualCandidate("residual_24h", "residual_24h"),
    ResidualCandidate("residual_blend", "residual_blend"),
    ResidualCandidate(
        "residual_24h_low_dispersion_50",
        "residual_24h",
        "p50",
    ),
    ResidualCandidate(
        "residual_24h_low_dispersion_70",
        "residual_24h",
        "p70",
    ),
    ResidualCandidate(
        "residual_24h_persistent",
        "residual_24h",
        require_persistence=True,
    ),
    ResidualCandidate(
        "residual_blend_low_dispersion_70",
        "residual_blend",
        "p70",
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark survivorship-aware residual momentum conditioned on "
            "lagged cross-sectional dispersion."
        )
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--panel", type=Path, default=DEFAULT_PANEL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--minimum-age-days", type=int, default=30)
    return parser.parse_args()


def add_residual_features(panel: pd.DataFrame) -> pd.DataFrame:
    result = panel.sort_values(["available_ms", "symbol"]).copy()
    btc = result.loc[result.symbol.eq("BTCUSDT")].set_index("available_ms")
    for hours in (6, 24, 72):
        market = btc[f"ret_{hours}h"]
        result[f"btc_ret_{hours}h"] = result.available_ms.map(market)
        result[f"residual_{hours}h"] = (
            result[f"ret_{hours}h"] - result[f"btc_ret_{hours}h"]
        )
    eligible = result.loc[
        ~result.symbol.isin({"BTCUSDT", "ETHUSDT"})
        & result.liquidity_24h.ge(5_000_000)
    ]
    state = eligible.groupby("available_ms", sort=True).agg(
        market_breadth_24h=("ret_24h", "median"),
        cross_sectional_dispersion_24h=("residual_24h", "std"),
        universe_size=("symbol", "nunique"),
    )
    state["dispersion_p50_lagged"] = (
        state.cross_sectional_dispersion_24h
        .rolling(720, min_periods=240)
        .quantile(0.50)
        .shift(1)
    )
    state["dispersion_p70_lagged"] = (
        state.cross_sectional_dispersion_24h
        .rolling(720, min_periods=240)
        .quantile(0.70)
        .shift(1)
    )
    for column in state.columns:
        result[column] = result.available_ms.map(state[column])
    result["market_direction"] = np.select(
        [
            result.market_breadth_24h.gt(0)
            & result.btc_ret_24h.gt(0),
            result.market_breadth_24h.lt(0)
            & result.btc_ret_24h.lt(0),
        ],
        ["LONG", "SHORT"],
        default="MIXED",
    )
    return result


def residual_signals(
    panel: pd.DataFrame,
    candidate: ResidualCandidate,
    minimum_age_days: int,
) -> pd.DataFrame:
    eligible = panel.loc[
        ~panel.symbol.isin({"BTCUSDT", "ETHUSDT"})
        & panel.symbol_age_days.ge(minimum_age_days)
        & panel.liquidity_24h.ge(candidate.minimum_liquidity_24h)
        & panel.universe_size.ge(60)
        & panel.market_direction.ne("MIXED")
    ].copy()
    if candidate.dispersion_gate == "p50":
        eligible = eligible.loc[
            eligible.cross_sectional_dispersion_24h.le(
                eligible.dispersion_p50_lagged
            )
        ]
    elif candidate.dispersion_gate == "p70":
        eligible = eligible.loc[
            eligible.cross_sectional_dispersion_24h.le(
                eligible.dispersion_p70_lagged
            )
        ]
    if candidate.require_persistence:
        eligible = eligible.loc[
            np.where(
                eligible.market_direction.eq("LONG"),
                eligible.residual_6h.gt(0),
                eligible.residual_6h.lt(0),
            )
        ]
    for hours in (6, 24, 72):
        eligible[f"residual_{hours}h_rank"] = eligible.groupby(
            "available_ms",
            sort=False,
        )[f"residual_{hours}h"].rank(pct=True)
    if candidate.score == "residual_blend":
        eligible["residual_score"] = eligible[
            [
                "residual_6h_rank",
                "residual_24h_rank",
                "residual_72h_rank",
            ]
        ].mean(axis=1)
    else:
        eligible["residual_score"] = eligible.residual_24h_rank
    eligible["directional_score"] = np.where(
        eligible.market_direction.eq("LONG"),
        eligible.residual_score,
        1.0 - eligible.residual_score,
    )
    selected = (
        eligible.sort_values(
            ["available_ms", "directional_score", "liquidity_24h"],
            ascending=[True, False, False],
        )
        .groupby("available_ms", sort=False)
        .head(1)
        .copy()
    )
    selected["direction"] = selected.market_direction
    selected["strength"] = selected.directional_score
    return selected.sort_values("available_ms").reset_index(drop=True)


def evaluate_candidate(
    panel: pd.DataFrame,
    candidate: ResidualCandidate,
    minimum_age_days: int,
) -> tuple[dict[str, Any], pd.DataFrame]:
    signals = residual_signals(panel, candidate, minimum_age_days)
    trades = simulate(signals, panel, PROFILE, cost_pct=BASE_COST)
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
    qualified = bool(
        dev_base.get("trades", 0) >= 30
        and min(
            item["base"].get("trades", 0)
            for item in fold_reports.values()
        )
        >= 8
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
            "folds": fold_reports,
        },
        "development_qualified": qualified,
    }, trades


def frozen_report(trades: pd.DataFrame) -> dict[str, Any]:
    stressed = cost_adjusted(trades, STRESS_COST)
    windows = {
        "validation_apr_may": ("2026-04-01", "2026-06-01"),
        "test_june": ("2026-06-01", "2026-07-01"),
        "final_july": ("2026-07-01", "2026-08-01"),
    }
    report: dict[str, Any] = {}
    for name, (start, end) in windows.items():
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
    panel = add_residual_features(panel)
    reports: dict[str, dict[str, Any]] = {}
    trades_by_name: dict[str, pd.DataFrame] = {}
    for candidate in CANDIDATES:
        report, trades = evaluate_candidate(
            panel,
            candidate,
            args.minimum_age_days,
        )
        reports[candidate.name] = report
        trades_by_name[candidate.name] = trades
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
    frozen = (
        frozen_report(trades_by_name[selected[0]]) if selected else None
    )
    qualified = bool(
        frozen
        and all(
            frozen[name]["base"].get("profit_factor", 0) > 1.05
            and frozen[name]["stress"].get("profit_factor", 0) > 1.0
            and frozen[name]["base"].get("trades", 0) >= 20
            for name in frozen
        )
    )
    result = {
        "experiment": "s0_point_in_time_residual_momentum",
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
    (args.output / "report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
