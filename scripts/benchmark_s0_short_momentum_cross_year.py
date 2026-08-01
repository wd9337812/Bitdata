from __future__ import annotations

import argparse
import gc
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_s0_adaptive_30d_momentum import (  # noqa: E402
    DEFAULT_DATA,
    adjusted_cost,
    apply_event_time_gate,
    report,
)
from scripts.benchmark_s0_cross_sectional_momentum import (  # noqa: E402
    PROFILES,
    Profile,
    rank_signals,
    simulate,
)
from scripts.benchmark_s0_xmom_point_in_time import (  # noqa: E402
    build_panel,
    load_manifest,
)


DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_short_momentum_cross_year"
PROFILE_NAMES = ("momentum_24h_5pct", "momentum_72h_5pct")
BASE_COST_PCT = 0.36
STRESS_COST_PCT = 0.60


def selected_profiles() -> tuple[Profile, ...]:
    return tuple(profile for profile in PROFILES if profile.name in PROFILE_NAMES)


def build_independent_paths(
    data_dirs: tuple[Path, ...],
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    path_parts = {profile.name: [] for profile in selected_profiles()}
    signal_parts = {profile.name: [] for profile in selected_profiles()}
    for data_dir in data_dirs:
        starts, _ = load_manifest(data_dir)
        panel = build_panel(data_dir, starts)
        for profile in selected_profiles():
            signals = rank_signals(panel, profile)
            signal_parts[profile.name].append(signals.copy())
            path_parts[profile.name].append(
                simulate(
                    signals,
                    panel,
                    profile,
                    cost_pct=0.0,
                    enforce_single_position=False,
                )
            )
        del panel
        gc.collect()
    paths = {
        name: pd.concat(parts, ignore_index=True)
        .sort_values(["entry_ms", "strength"], ascending=[True, False])
        .reset_index(drop=True)
        for name, parts in path_parts.items()
    }
    signals = {
        name: pd.concat(parts, ignore_index=True)
        .sort_values(["available_ms", "symbol"])
        .drop_duplicates(["available_ms", "symbol", "direction"], keep="last")
        .reset_index(drop=True)
        for name, parts in signal_parts.items()
    }
    return paths, signals


def scenario(
    paths: pd.DataFrame,
    cost_pct: float,
    symbol_embargo_hours: int,
) -> dict[str, Any]:
    paper = adjusted_cost(paths, cost_pct)
    return {
        "independent_paper": report(paper),
        "funded": report(apply_event_time_gate(paper)),
        "funded_symbol_embargo": report(
            apply_event_time_gate(
                paper,
                symbol_embargo_hours=symbol_embargo_hours,
            )
        ),
    }


def qualifies(stress: dict[str, Any]) -> bool:
    funded = stress["funded_symbol_embargo"]
    overall = funded["overall"]
    annual = funded["annual"]
    diversified = funded["without_top_3_symbols"]
    bootstrap = funded["bootstrap"]
    required_years = {str(year) for year in range(2021, 2027)}
    return bool(
        required_years.issubset(annual)
        and all(
            annual[year].get("net_pct_points", 0) > 0
            and annual[year].get("profit_factor", 0) > 1
            for year in required_years
        )
        and overall.get("trades", 0) >= 100
        and overall.get("symbols", 0) >= 20
        and overall.get("profit_factor", 0) > 1.2
        and diversified.get("profit_factor", 0) > 1
        and diversified.get("net_pct_points", 0) > 0
        and bootstrap.get("positive_probability", 0) >= 0.95
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cross-year audit of frozen 24h and 72h momentum rules."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    paths: dict[str, pd.DataFrame] = {}
    signals: dict[str, pd.DataFrame] = {}
    rebuild = args.rebuild or any(
        not (args.output / f"{name}_paths.parquet").exists()
        for name in PROFILE_NAMES
    )
    if rebuild:
        paths, signals = build_independent_paths(DEFAULT_DATA)
        for name in PROFILE_NAMES:
            paths[name].to_parquet(
                args.output / f"{name}_paths.parquet", index=False
            )
            signals[name].to_parquet(
                args.output / f"{name}_signals.parquet", index=False
            )
    else:
        for name in PROFILE_NAMES:
            paths[name] = pd.read_parquet(args.output / f"{name}_paths.parquet")
            signals[name] = pd.read_parquet(
                args.output / f"{name}_signals.parquet"
            )

    candidates: dict[str, Any] = {}
    qualified: list[str] = []
    for profile in selected_profiles():
        embargo_hours = 24 if profile.name == "momentum_24h_5pct" else 72
        scenarios = {
            f"cost_{cost:.2f}pct": scenario(
                paths[profile.name], cost, embargo_hours
            )
            for cost in (BASE_COST_PCT, STRESS_COST_PCT)
        }
        stress = scenarios[f"cost_{STRESS_COST_PCT:.2f}pct"]
        eligible = qualifies(stress)
        if eligible:
            qualified.append(profile.name)
        candidates[profile.name] = {
            "profile": profile.__dict__,
            "symbol_embargo_hours": embargo_hours,
            "signals": int(len(signals[profile.name])),
            "independent_paths": int(len(paths[profile.name])),
            "scenarios": scenarios,
            "qualified_for_minute_replay": eligible,
        }
    result = {
        "experiment": "s0_short_momentum_cross_year",
        "selection_history": (
            "The 24h and 72h rules were frozen in the earlier 2026 study; "
            "2021-2025 provide previously unused cross-year evidence."
        ),
        "candidates": candidates,
        "qualified_candidates": qualified,
        "decision": (
            "minute_replay_required" if qualified else "reject_before_minute_replay"
        ),
        "warning": "Historical qualification is not live-trading approval.",
    }
    (args.output / "report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
