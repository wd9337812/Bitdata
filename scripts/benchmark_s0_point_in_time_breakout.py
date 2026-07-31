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

from scripts.audit_s0_xmom_regime_candidates import (
    BASE_COST,
    STRESS_COST,
    block_bootstrap,
    choose_frozen_candidate,
    cost_adjusted,
    development_folds,
    scope,
)
from scripts.benchmark_s0_cross_sectional_momentum import (
    Profile,
    simulate,
    summarize,
)
from scripts.benchmark_s0_xmom_point_in_time import (
    DEFAULT_DATA,
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
    ROOT / "data" / "research" / "s0_point_in_time_breakout"
)


@dataclass(frozen=True)
class BreakoutCandidate:
    name: str
    channel_hours: int
    minimum_volume_ratio: float
    minimum_liquidity_24h: float
    stop_atr: float
    reward_r: float
    hold_hours: int

    def profile(self) -> Profile:
        return Profile(
            name=self.name,
            score_columns=("ret_24h",),
            quantile=0.0,
            stop_atr=self.stop_atr,
            reward_r=self.reward_r,
            hold_hours=self.hold_hours,
        )


CANDIDATES = (
    BreakoutCandidate("breakout_24h", 24, 1.0, 20_000_000, 1.5, 2.0, 24),
    BreakoutCandidate("breakout_48h", 48, 1.0, 20_000_000, 1.5, 2.0, 36),
    BreakoutCandidate("breakout_72h", 72, 1.0, 20_000_000, 1.8, 2.0, 48),
    BreakoutCandidate(
        "breakout_24h_volume",
        24,
        1.5,
        20_000_000,
        1.5,
        2.0,
        24,
    ),
    BreakoutCandidate(
        "breakout_48h_volume",
        48,
        1.5,
        20_000_000,
        1.5,
        2.0,
        36,
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark time-series Donchian-style breakouts on the official "
            "point-in-time Binance USD-M universe."
        )
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--panel", type=Path, default=DEFAULT_PANEL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--minimum-age-days", type=int, default=30)
    return parser.parse_args()


def add_breakout_features(
    panel: pd.DataFrame,
    channel_hours: tuple[int, ...] = (24, 48, 72),
) -> pd.DataFrame:
    result = panel.sort_values(["symbol", "available_ms"]).copy()
    grouped = result.groupby("symbol", sort=False)
    result["prior_quote_volume_mean_24h"] = grouped.quote_volume.transform(
        lambda values: values.rolling(24, min_periods=20).mean().shift(1)
    )
    result["volume_ratio"] = (
        result.quote_volume / result.prior_quote_volume_mean_24h
    )
    for hours in channel_hours:
        result[f"channel_high_{hours}h"] = grouped.high.transform(
            lambda values: values.rolling(hours, min_periods=hours).max().shift(1)
        )
        result[f"channel_low_{hours}h"] = grouped.low.transform(
            lambda values: values.rolling(hours, min_periods=hours).min().shift(1)
        )
    return result.sort_values(["available_ms", "symbol"]).reset_index(drop=True)


def raw_breakout_signals(
    panel: pd.DataFrame,
    candidate: BreakoutCandidate,
    minimum_age_days: int,
) -> pd.DataFrame:
    high_column = f"channel_high_{candidate.channel_hours}h"
    low_column = f"channel_low_{candidate.channel_hours}h"
    valid = (
        panel.symbol_age_days.ge(minimum_age_days)
        & panel.liquidity_24h.ge(candidate.minimum_liquidity_24h)
        & panel.volume_ratio.ge(candidate.minimum_volume_ratio)
        & panel[high_column].notna()
        & panel[low_column].notna()
        & panel.atr_24h.gt(0)
    )
    working = panel.copy()
    working["long_active"] = working.close.gt(working[high_column])
    working["short_active"] = working.close.lt(working[low_column])
    grouped = working.groupby("symbol", sort=False)
    previous_long = grouped.long_active.shift(1).fillna(False)
    previous_short = grouped.short_active.shift(1).fillna(False)
    working["long_trigger"] = working.long_active & ~previous_long
    working["short_trigger"] = working.short_active & ~previous_short
    triggered = working.loc[
        valid & (working.long_trigger | working.short_trigger)
    ].copy()
    triggered["direction"] = np.where(
        triggered.long_trigger,
        "LONG",
        "SHORT",
    )
    triggered["breakout_atr"] = np.where(
        triggered.direction.eq("LONG"),
        (triggered.close - triggered[high_column]) / triggered.atr_24h,
        (triggered[low_column] - triggered.close) / triggered.atr_24h,
    )
    triggered["liquidity_rank"] = triggered.groupby(
        "available_ms",
        sort=False,
    ).liquidity_24h.rank(pct=True)
    triggered["volume_rank"] = triggered.groupby(
        "available_ms",
        sort=False,
    ).volume_ratio.rank(pct=True)
    triggered["breakout_rank"] = triggered.groupby(
        "available_ms",
        sort=False,
    ).breakout_atr.rank(pct=True)
    triggered["auction_score"] = (
        0.50 * triggered.breakout_rank
        + 0.30 * triggered.volume_rank
        + 0.20 * triggered.liquidity_rank
    )
    return triggered


def auction_signals(raw: pd.DataFrame) -> pd.DataFrame:
    if raw.empty:
        return raw.copy()
    selected = (
        raw.sort_values(
            [
                "available_ms",
                "auction_score",
                "breakout_atr",
                "liquidity_24h",
            ],
            ascending=[True, False, False, False],
        )
        .groupby("available_ms", sort=False)
        .head(1)
        .copy()
    )
    selected["market_direction"] = selected.direction
    selected["strength"] = selected.auction_score
    return selected.sort_values("available_ms").reset_index(drop=True)


def candidate_development_report(
    trades: pd.DataFrame,
    signal_count: int,
) -> dict[str, Any]:
    stressed = cost_adjusted(trades, STRESS_COST)
    development = scope(trades, "2026-02-01", "2026-04-01")
    development_stress = scope(stressed, "2026-02-01", "2026-04-01")
    folds = development_folds(trades)
    stress_folds = development_folds(stressed)
    fold_report = {
        name: {
            "base": summarize(frame),
            "stress": summarize(stress_folds[name]),
        }
        for name, frame in folds.items()
    }
    positive_base_folds = sum(
        item["base"].get("net_pct_points", 0) > 0
        for item in fold_report.values()
    )
    positive_stress_folds = sum(
        item["stress"].get("net_pct_points", 0) > 0
        for item in fold_report.values()
    )
    base = summarize(development)
    stress = summarize(development_stress)
    qualifies = (
        base.get("trades", 0) >= 30
        and min(
            item["base"].get("trades", 0)
            for item in fold_report.values()
        )
        >= 8
        and positive_base_folds >= 2
        and positive_stress_folds >= 2
        and base.get("profit_factor", 0) >= 1.10
        and stress.get("profit_factor", 0) >= 1.02
    )
    return {
        "signals": signal_count,
        "development": {
            "base": base,
            "stress": stress,
            "positive_base_folds": positive_base_folds,
            "positive_stress_folds": positive_stress_folds,
            "folds": fold_report,
        },
        "development_qualified": bool(qualifies),
    }


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
        }
        if name == "final_july":
            report[name]["weekly_block_bootstrap_base"] = block_bootstrap(base)
            report[name]["weekly_block_bootstrap_stress"] = block_bootstrap(
                stress
            )
    frozen = scope(trades, "2026-04-01", "2026-08-01")
    frozen_stress = scope(stressed, "2026-04-01", "2026-08-01")
    report["all_frozen_apr_july"] = {
        "base": summarize(frozen),
        "stress": summarize(frozen_stress),
    }
    return report


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    _, manifest = load_manifest(args.data)
    panel = pd.read_parquet(args.panel)
    panel = add_breakout_features(panel)
    candidate_reports: dict[str, dict[str, Any]] = {}
    trades_by_name: dict[str, pd.DataFrame] = {}
    for candidate in CANDIDATES:
        raw = raw_breakout_signals(
            panel,
            candidate,
            args.minimum_age_days,
        )
        signals = auction_signals(raw)
        trades = simulate(
            signals,
            panel,
            candidate.profile(),
            cost_pct=BASE_COST,
        )
        candidate_reports[candidate.name] = {
            "parameters": asdict(candidate),
            **candidate_development_report(trades, len(signals)),
        }
        trades_by_name[candidate.name] = trades
    frozen = choose_frozen_candidate(candidate_reports)
    evidence = frozen_report(trades_by_name[frozen]) if frozen else {}
    frozen_passes = bool(
        frozen
        and evidence["validation_apr_may"]["base"].get("net_pct_points", 0) > 0
        and evidence["test_june"]["base"].get("net_pct_points", 0) > 0
        and evidence["final_july"]["base"].get("net_pct_points", 0) > 0
        and evidence["all_frozen_apr_july"]["stress"].get(
            "profit_factor",
            0,
        )
        > 1.0
    )
    report = {
        "method": (
            "Time-series channel breakout using only completed bars, first "
            "crossing, next-hour open entry, conservative stop-first OHLC, "
            "ATR exits, and point-in-time symbol availability."
        ),
        "references": [
            "Moskowitz, Ooi and Pedersen (2012), Time Series Momentum",
            "Binance official public data archive with SHA-256 checksums",
            "Freqtrade lookahead-analysis methodology",
        ],
        "data": {
            "monthly_source": manifest.get("source"),
            "daily_extension": manifest.get("daily_extension"),
            "rows": int(len(panel)),
            "symbols": int(panel.symbol.nunique()),
        },
        "cost_pct": {"base": BASE_COST, "stress": STRESS_COST},
        "candidate_selection": candidate_reports,
        "frozen_candidate": frozen,
        "frozen_evaluation": evidence,
        "hourly_research_passed": frozen_passes,
        "live_qualified": False,
        "live_qualification_reason": (
            "Minute-level execution and shadow validation are still required."
            if frozen_passes
            else "Frozen out-of-sample windows did not all pass."
        ),
    }
    if frozen:
        frame = trades_by_name[frozen].copy()
        frame["candidate"] = frozen
        frame.to_parquet(
            args.output / "frozen_candidate_trades.parquet",
            index=False,
        )
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
