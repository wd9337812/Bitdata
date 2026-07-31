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
from scripts.benchmark_s0_cross_sectional_momentum import summarize
from scripts.benchmark_s0_point_in_time_breakout import (
    DEFAULT_PANEL,
    add_breakout_features,
)
from scripts.benchmark_s0_xmom_point_in_time import (
    DEFAULT_DATA,
    load_manifest,
)


DEFAULT_OUTPUT = (
    ROOT / "data" / "research" / "s0_point_in_time_reversal"
)


@dataclass(frozen=True)
class ReversalCandidate:
    name: str
    formation_hours: int
    shock_z: float
    minimum_volume_ratio: float
    minimum_liquidity_24h: float
    stop_atr: float
    target_fraction: float
    hold_hours: int
    minimum_target_pct: float = 0.35


CANDIDATES = (
    ReversalCandidate("reversal_1h_z2_5", 1, 2.5, 1.0, 20_000_000, 1.5, 0.5, 4),
    ReversalCandidate("reversal_1h_z3", 1, 3.0, 1.0, 20_000_000, 1.5, 0.5, 4),
    ReversalCandidate("reversal_2h_z2_5", 2, 2.5, 1.0, 20_000_000, 1.5, 0.5, 6),
    ReversalCandidate("reversal_4h_z2_5", 4, 2.5, 1.0, 20_000_000, 1.8, 0.5, 8),
    ReversalCandidate(
        "reversal_1h_z2_5_volume",
        1,
        2.5,
        1.5,
        20_000_000,
        1.5,
        0.5,
        4,
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark cost-aware short-horizon reversal after volatility-"
            "normalized crypto price shocks."
        )
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--panel", type=Path, default=DEFAULT_PANEL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--minimum-age-days", type=int, default=30)
    return parser.parse_args()


def add_reversal_features(panel: pd.DataFrame) -> pd.DataFrame:
    result = add_breakout_features(panel, ())
    result = result.sort_values(["symbol", "available_ms"]).copy()
    grouped = result.groupby("symbol", sort=False)
    result["ret_1h"] = grouped.close.pct_change()
    result["prior_volatility_48h"] = grouped.ret_1h.transform(
        lambda values: values.rolling(48, min_periods=36).std().shift(1)
    )
    for hours in (1, 2, 4):
        result[f"formation_reference_{hours}h"] = grouped.close.shift(hours)
        result[f"formation_return_{hours}h"] = (
            result.close / result[f"formation_reference_{hours}h"] - 1.0
        )
        result[f"shock_z_{hours}h"] = (
            result[f"formation_return_{hours}h"]
            / (result.prior_volatility_48h * np.sqrt(hours))
        )
    return result.sort_values(["available_ms", "symbol"]).reset_index(drop=True)


def reversal_signals(
    panel: pd.DataFrame,
    candidate: ReversalCandidate,
    minimum_age_days: int,
) -> pd.DataFrame:
    z_column = f"shock_z_{candidate.formation_hours}h"
    return_column = f"formation_return_{candidate.formation_hours}h"
    reference_column = (
        f"formation_reference_{candidate.formation_hours}h"
    )
    eligible = panel.loc[
        panel.symbol_age_days.ge(minimum_age_days)
        & panel.liquidity_24h.ge(candidate.minimum_liquidity_24h)
        & panel.volume_ratio.ge(candidate.minimum_volume_ratio)
        & panel[z_column].abs().ge(candidate.shock_z)
        & panel.atr_24h.gt(0)
        & panel[reference_column].notna()
    ].copy()
    eligible["direction"] = np.where(
        eligible[return_column].gt(0),
        "SHORT",
        "LONG",
    )
    eligible["shock_strength"] = eligible[z_column].abs()
    eligible["shock_rank"] = eligible.groupby(
        "available_ms",
        sort=False,
    ).shock_strength.rank(pct=True)
    eligible["volume_rank"] = eligible.groupby(
        "available_ms",
        sort=False,
    ).volume_ratio.rank(pct=True)
    eligible["liquidity_rank"] = eligible.groupby(
        "available_ms",
        sort=False,
    ).liquidity_24h.rank(pct=True)
    eligible["auction_score"] = (
        0.60 * eligible.shock_rank
        + 0.25 * eligible.volume_rank
        + 0.15 * eligible.liquidity_rank
    )
    selected = (
        eligible.sort_values(
            [
                "available_ms",
                "auction_score",
                "shock_strength",
                "liquidity_24h",
            ],
            ascending=[True, False, False, False],
        )
        .groupby("available_ms", sort=False)
        .head(1)
        .copy()
    )
    selected["formation_reference"] = selected[reference_column]
    selected["market_direction"] = selected.direction
    selected["strength"] = selected.auction_score
    return selected.sort_values("available_ms").reset_index(drop=True)


def simulate_reversal(
    signals: pd.DataFrame,
    panel: pd.DataFrame,
    candidate: ReversalCandidate,
    cost_pct: float = BASE_COST,
) -> pd.DataFrame:
    bars = {
        symbol: scoped.sort_values("available_ms").set_index("available_ms")
        for symbol, scoped in panel.groupby("symbol", sort=False)
    }
    trades: list[dict[str, Any]] = []
    next_available_ms = -1
    for signal in signals.itertuples(index=False):
        if int(signal.available_ms) < next_available_ms:
            continue
        scoped = bars.get(signal.symbol)
        entry_bar_ms = int(signal.available_ms) + 3_600_000
        if scoped is None or entry_bar_ms not in scoped.index:
            continue
        start = int(scoped.index.searchsorted(entry_bar_ms))
        path = scoped.iloc[start : start + candidate.hold_hours]
        if path.empty:
            continue
        entry = float(path.iloc[0].open)
        atr = float(signal.atr_24h)
        reference = float(signal.formation_reference)
        sign = 1.0 if signal.direction == "LONG" else -1.0
        if (
            not np.isfinite(entry)
            or not np.isfinite(atr)
            or not np.isfinite(reference)
            or entry <= 0
            or atr <= 0
            or sign * (reference - entry) <= 0
        ):
            continue
        stop_distance = min(candidate.stop_atr * atr, entry * 0.15)
        target_distance = abs(reference - entry) * candidate.target_fraction
        target_pct = target_distance / entry * 100.0
        if target_pct < candidate.minimum_target_pct:
            continue
        stop = entry - sign * stop_distance
        take = entry + sign * target_distance
        exit_price = float(path.iloc[-1].close)
        exit_ms = int(path.index[-1]) + 3_600_000
        outcome = "TIME"
        for timestamp, bar in path.iterrows():
            if sign > 0 and bar.open <= stop:
                exit_price, outcome = float(bar.open), "STOP_GAP"
            elif sign < 0 and bar.open >= stop:
                exit_price, outcome = float(bar.open), "STOP_GAP"
            elif sign > 0 and bar.open >= take:
                exit_price, outcome = take, "TAKE_GAP"
            elif sign < 0 and bar.open <= take:
                exit_price, outcome = take, "TAKE_GAP"
            else:
                stop_hit = bar.low <= stop if sign > 0 else bar.high >= stop
                take_hit = bar.high >= take if sign > 0 else bar.low <= take
                if stop_hit:
                    exit_price, outcome = stop, "STOP"
                elif take_hit:
                    exit_price, outcome = take, "TAKE"
                else:
                    continue
            exit_ms = int(timestamp) + 3_600_000
            break
        gross_pct = sign * (exit_price / entry - 1.0) * 100.0
        trades.append(
            {
                "profile": candidate.name,
                "signal_ms": int(signal.available_ms),
                "entry_ms": entry_bar_ms,
                "exit_ms": exit_ms,
                "symbol": signal.symbol,
                "direction": signal.direction,
                "market_direction": signal.market_direction,
                "strength": float(signal.strength),
                "entry": entry,
                "exit": exit_price,
                "stop": stop,
                "take": take,
                "target_pct": target_pct,
                "outcome": outcome,
                "gross_pct": gross_pct,
                "cost_pct": cost_pct,
                "net_pct": gross_pct - cost_pct,
            }
        )
        next_available_ms = exit_ms
    return pd.DataFrame(trades)


def development_report(
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
        base.get("trades", 0) >= 45
        and min(
            item["base"].get("trades", 0)
            for item in fold_report.values()
        )
        >= 12
        and positive_base_folds == 3
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
    result: dict[str, Any] = {}
    for name, (start, end) in windows.items():
        base = scope(trades, start, end)
        stress = scope(stressed, start, end)
        result[name] = {
            "base": summarize(base),
            "stress": summarize(stress),
        }
        if name == "final_july":
            result[name]["weekly_block_bootstrap_base"] = block_bootstrap(base)
            result[name]["weekly_block_bootstrap_stress"] = block_bootstrap(
                stress
            )
    frozen = scope(trades, "2026-04-01", "2026-08-01")
    frozen_stress = scope(stressed, "2026-04-01", "2026-08-01")
    result["all_frozen_apr_july"] = {
        "base": summarize(frozen),
        "stress": summarize(frozen_stress),
    }
    return result


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    _, manifest = load_manifest(args.data)
    panel = add_reversal_features(pd.read_parquet(args.panel))
    reports: dict[str, dict[str, Any]] = {}
    trades_by_name: dict[str, pd.DataFrame] = {}
    for candidate in CANDIDATES:
        signals = reversal_signals(
            panel,
            candidate,
            args.minimum_age_days,
        )
        trades = simulate_reversal(signals, panel, candidate)
        reports[candidate.name] = {
            "parameters": asdict(candidate),
            **development_report(trades, len(signals)),
        }
        trades_by_name[candidate.name] = trades
    frozen = choose_frozen_candidate(reports)
    evidence = frozen_report(trades_by_name[frozen]) if frozen else {}
    passed = bool(
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
            "Opposite-direction entry after an hourly volatility-normalized "
            "shock, next-hour open, partial reversion target, ATR stop, "
            "conservative stop-first OHLC, point-in-time symbol universe."
        ),
        "references": [
            "Wen et al. (2022), Intraday return predictability in cryptocurrency markets",
            "De Nicola (2021), On the Intraday Behavior of Bitcoin",
            "Binance official public data archive with SHA-256 checksums",
        ],
        "data": {
            "monthly_source": manifest.get("source"),
            "daily_extension": manifest.get("daily_extension"),
            "rows": int(len(panel)),
            "symbols": int(panel.symbol.nunique()),
        },
        "cost_pct": {"base": BASE_COST, "stress": STRESS_COST},
        "candidate_selection": reports,
        "frozen_candidate": frozen,
        "frozen_evaluation": evidence,
        "hourly_research_passed": passed,
        "live_qualified": False,
        "live_qualification_reason": (
            "Minute-level execution and shadow validation are still required."
            if passed
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
