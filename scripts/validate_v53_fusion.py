from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from analyze_vps_strategy_history import TEST_START, VALIDATION_START, _metrics, schedule


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HISTORY = ROOT / "data" / "research" / "vps_history_20260727"
DEFAULT_REPLAY = ROOT / "data" / "research" / "v53_validation_20260729"
DEFAULT_OUTPUT = DEFAULT_REPLAY / "v53_fusion_validation.json"
SOURCE_VERSIONS = ("v4.10", "v4.11", "v4.7.2", "v4.7.3", "v4.7.4")
PROFILE = "stop_1.00_rr_1.00_hold_10"

WEIGHTS = {
    "cross_sectional_strength": 0.25,
    "volume_persistence": 0.15,
    "directed_flow": 0.10,
    "medium_path": 0.20,
    "entry_quality": 0.20,
    "liquidity": 0.10,
}
SETUP_ADJUSTMENTS = {
    "momentum": -0.08,
    "breakout": 0.05,
    "pullback": 0.07,
    "prebreakout": 0.03,
}


def _load(history: Path, replay: Path) -> pd.DataFrame:
    source = pd.read_csv(history / "shadow_trades_compact.csv.gz", low_memory=False)
    source = source[
        source.strategy_family.eq("extreme_v4_roll")
        & source.strategy_version.isin(SOURCE_VERSIONS)
        & source.evidence_type.eq("decision")
        & source.status.eq("CLOSED")
    ].copy()
    feature_columns = [
        "id",
        "entry",
        "stop",
        "take_profit",
        "estimated_cost",
        "notional",
        "medium_trend_aligned",
        "entry_phase",
        *WEIGHTS,
        "anti_chase",
        "regime_fit",
        "spread_pct",
        "depth_notional",
    ]
    replayed = pd.read_parquet(replay / "replayed_candidates.parquet")
    replayed = replayed[replayed.profile.eq(PROFILE)].copy()
    return replayed.merge(source[feature_columns], on="id", how="left", validate="many_to_one")


def _prepare(frame: pd.DataFrame) -> pd.DataFrame:
    numeric = [
        "entry",
        "stop",
        "take_profit",
        "estimated_cost",
        "notional",
        *WEIGHTS,
        "anti_chase",
        "regime_fit",
        "spread_pct",
        "depth_notional",
    ]
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)

    quality = sum(frame[name] * weight for name, weight in WEIGHTS.items())
    quality += frame.setup_type.map(SETUP_ADJUSTMENTS).fillna(0.0)
    frame["v53_quality"] = quality.clip(0.0, 1.0)
    frame["batch"] = (frame.opened_ms // 300_000).astype("int64")
    frame["v53_rank_percentile"] = frame.groupby("batch").v53_quality.rank(
        pct=True,
        method="average",
    )

    trend_route = (
        (frame.market_regime.eq("broad_up") & frame.direction.eq("LONG"))
        | (frame.market_regime.eq("broad_down") & frame.direction.eq("SHORT"))
    )
    neutral_route = frame.market_regime.isin(("quiet", "mixed", "rotation")) & frame[
        "medium_trend_aligned"
    ].fillna(False)
    setup_route = frame.setup_type.isin(("breakout", "prebreakout", "pullback"))
    entry_route = ~frame.setup_type.eq("pullback") | frame.entry_phase.eq("RETEST")
    frame["route_passed"] = (trend_route | neutral_route) & setup_route & entry_route

    frame["confirmations"] = (
        frame.volume_persistence.ge(0.55).astype(int)
        + frame.directed_flow.ge(0.70).astype(int)
        + frame.regime_fit.ge(0.80).astype(int)
        + (
            frame.medium_path.ge(0.45)
            | frame.medium_trend_aligned.fillna(False)
        ).astype(int)
        + frame.anti_chase.ge(0.60).astype(int)
    )
    frame["reward_pct"] = (frame.take_profit - frame.entry).abs().div(frame.entry).mul(100.0)
    frame["stop_pct"] = (frame.stop - frame.entry).abs().div(frame.entry).mul(100.0)
    reported_cost = (
        frame.estimated_cost.div(frame.notional.replace(0.0, np.nan)).mul(100.0).fillna(0.12)
    )
    frame["model_cost_pct"] = np.maximum(
        reported_cost,
        0.08 + 0.04 + frame.spread_pct + 0.02,
    )
    frame["gross_cost_multiple"] = frame.reward_pct.div(
        frame.model_cost_pct.replace(0.0, np.nan)
    )
    win_probability = (0.28 + frame.v53_quality * 0.36).clip(0.30, 0.62)
    frame["model_expected_net_pct"] = (
        win_probability * frame.reward_pct
        - (1.0 - win_probability) * frame.stop_pct
        - frame.model_cost_pct
    )
    frame["model_lower_expected_net_pct"] = frame.model_expected_net_pct - (
        0.04 + (1.0 - frame.v53_quality) * 0.12
    )
    frame["rank_floor"] = np.select(
        [frame.market_regime.eq("panic"), frame.market_regime.eq("quiet")],
        [0.95, 0.90],
        default=0.85,
    )
    return frame


def _funnel(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    stages: list[tuple[str, pd.Series]] = [
        ("source_candidates", pd.Series(True, index=frame.index)),
        ("route", frame.route_passed),
        (
            "rank",
            frame.route_passed
            & frame.v53_rank_percentile.ge(frame.rank_floor),
        ),
        (
            "cross_sectional",
            frame.route_passed
            & frame.v53_rank_percentile.ge(frame.rank_floor)
            & frame.cross_sectional_strength.ge(0.72),
        ),
    ]
    base = stages[-1][1]
    stages.extend(
        [
            ("quality", base & frame.v53_quality.ge(0.56)),
            (
                "expectancy",
                base
                & frame.v53_quality.ge(0.56)
                & frame.model_expected_net_pct.ge(0.03)
                & frame.model_lower_expected_net_pct.ge(-0.03),
            ),
        ]
    )
    base = stages[-1][1]
    stages.extend(
        [
            ("cost", base & frame.gross_cost_multiple.ge(3.50)),
            (
                "confirmations",
                base
                & frame.gross_cost_multiple.ge(3.50)
                & frame.confirmations.ge(2),
            ),
        ]
    )
    admitted = (
        stages[-1][1]
        & frame.spread_pct.le(0.10)
        & frame.depth_notional.ge(750.0)
    )
    stages.append(("liquidity", admitted))
    return frame[admitted].copy(), {
        name: int(frame.loc[mask, "id"].nunique())
        for name, mask in stages
    }


def _window(frame: pd.DataFrame, name: str) -> pd.DataFrame:
    if name == "development":
        return frame[frame.time < VALIDATION_START]
    if name == "validation":
        return frame[(frame.time >= VALIDATION_START) & (frame.time < TEST_START)]
    return frame[frame.time >= TEST_START]


def _group_metrics(frame: pd.DataFrame, column: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for value, scoped in frame.groupby(column, dropna=False):
        selected = schedule(scoped, score_column="v53_rank_percentile")
        result[str(value)] = _metrics(selected.net_pct, str(value))
    return result


def validate(frame: pd.DataFrame) -> dict[str, Any]:
    admitted, funnel = _funnel(frame)
    windows: dict[str, Any] = {}
    for name in ("development", "validation", "test"):
        scoped = _window(admitted, name)
        selected = schedule(scoped, score_column="v53_rank_percentile")
        windows[name] = {
            **_metrics(selected.net_pct, name),
            "symbols": int(selected.symbol.nunique()),
            "market_regimes": int(selected.market_regime.nunique()),
            "by_direction": _group_metrics(scoped, "direction"),
            "by_setup": _group_metrics(scoped, "setup_type"),
            "by_regime": _group_metrics(scoped, "market_regime"),
        }
    validation = windows["validation"]
    test = windows["test"]
    passed = bool(
        validation.get("trades", 0) >= 30
        and test.get("trades", 0) >= 20
        and validation.get("net_pct_points", 0.0) > 0.0
        and test.get("net_pct_points", 0.0) > 0.0
        and validation.get("profit_factor", 0.0) > 1.0
        and test.get("profit_factor", 0.0) > 1.0
    )
    return {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "candidate_version": "v5.3",
        "source_versions": list(SOURCE_VERSIONS),
        "profile": PROFILE,
        "profile_note": "Closest available conservative replay to 0.9 ATR / 1.05R / 15m.",
        "funnel_unique_candidates": funnel,
        "admitted_unique_candidates": int(admitted.id.nunique()),
        "windows": windows,
        "acceptance": {
            "passed": passed,
            "decision": "eligible_for_guarded_live_canary" if passed else "shadow_only_no_auto_promotion",
            "requirements": {
                "validation_trades": 30,
                "test_trades": 20,
                "validation_and_test_net_positive": True,
                "validation_and_test_pf_above": 1.0,
            },
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the V5.3 fusion gates.")
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--replay", type=Path, default=DEFAULT_REPLAY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = validate(_prepare(_load(args.history, args.replay)))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
