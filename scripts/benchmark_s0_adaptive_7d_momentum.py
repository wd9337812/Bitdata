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
from scripts.benchmark_s0_cross_sectional_momentum import simulate  # noqa: E402
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


DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_7d_momentum_cross_year"
BASE_COST_PCT = 0.36
STRESS_COST_PCT = 0.60
SYMBOL_EMBARGO_HOURS = 7 * 24


def build_independent_paths(data_dirs: tuple[Path, ...]) -> tuple[pd.DataFrame, pd.DataFrame]:
    candidate = next(item for item in CANDIDATES if item.name == "momentum_7d_hold_1d")
    profile = profile_for(candidate)
    paths: list[pd.DataFrame] = []
    signal_parts: list[pd.DataFrame] = []
    for data_dir in data_dirs:
        starts, _ = load_manifest(data_dir)
        panel = add_slow_returns(build_panel(data_dir, starts))
        signals = slow_momentum_signals(panel, candidate, minimum_age_days=45)
        signal_parts.append(signals.copy())
        paths.append(
            simulate(
                signals,
                panel,
                profile,
                cost_pct=0.0,
                enforce_single_position=False,
            )
        )
        del panel, signals
        gc.collect()
    all_paths = (
        pd.concat(paths, ignore_index=True)
        .sort_values(["entry_ms", "strength"], ascending=[True, False])
        .reset_index(drop=True)
    )
    all_signals = (
        pd.concat(signal_parts, ignore_index=True)
        .sort_values(["available_ms", "symbol"])
        .drop_duplicates(["available_ms", "symbol", "direction"], keep="last")
        .reset_index(drop=True)
    )
    return all_paths, all_signals


def scenario(paths: pd.DataFrame, cost_pct: float) -> dict[str, Any]:
    paper = adjusted_cost(paths, cost_pct)
    funded = apply_event_time_gate(paper)
    embargo = apply_event_time_gate(
        paper,
        symbol_embargo_hours=SYMBOL_EMBARGO_HOURS,
    )
    return {
        "independent_paper": report(paper),
        "funded": report(funded),
        "funded_7d_symbol_embargo": report(embargo),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cross-year audit of the frozen 7d formation / 1d hold momentum rule."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    paths_file = args.output / "independent_hourly_paths.parquet"
    signals_file = args.output / "signals_all.parquet"
    if args.rebuild or not paths_file.exists() or not signals_file.exists():
        paths, signals = build_independent_paths(DEFAULT_DATA)
        paths.to_parquet(paths_file, index=False)
        signals.to_parquet(signals_file, index=False)
    else:
        paths = pd.read_parquet(paths_file)
        signals = pd.read_parquet(signals_file)

    scenarios = {
        f"cost_{cost:.2f}pct": scenario(paths, cost)
        for cost in (BASE_COST_PCT, STRESS_COST_PCT)
    }
    stress = scenarios[f"cost_{STRESS_COST_PCT:.2f}pct"]["funded_7d_symbol_embargo"]
    overall = stress["overall"]
    diversified = stress["without_top_3_symbols"]
    bootstrap = stress["bootstrap"]
    robust = bool(
        overall.get("trades", 0) >= 100
        and overall.get("symbols", 0) >= 20
        and overall.get("profit_factor", 0) > 1.2
        and overall.get("net_pct_points", 0) > 0
        and diversified.get("profit_factor", 0) > 1.0
        and diversified.get("net_pct_points", 0) > 0
        and bootstrap.get("positive_probability", 0) >= 0.95
    )
    result = {
        "experiment": "s0_7d_momentum_cross_year",
        "pre_registered_rule": {
            "formation_days": 7,
            "hold_days": 1,
            "stop_atr": 2.0,
            "reward_r": 2.0,
            "symbol_embargo_days": 7,
            "selection_history": (
                "Rule was frozen from the earlier 2026 development study; "
                "2021-2025 are used here as previously unused cross-year evidence."
            ),
        },
        "signals": int(len(signals)),
        "independent_paths": int(len(paths)),
        "scenarios": scenarios,
        "qualified_for_minute_replay": robust,
        "decision": "minute_replay_required" if robust else "reject_before_minute_replay",
        "warning": "Historical qualification is not live-trading approval.",
    }
    (args.output / "report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
