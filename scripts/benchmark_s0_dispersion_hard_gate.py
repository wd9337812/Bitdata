from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_s0_dispersion_scaled_momentum import (
    COSTS,
    DEFAULT_DATA,
    WINDOWS,
    combine_point_in_time_panels,
    dispersion_exposure,
    eligible_cross_section,
    metrics,
    simulate,
)


DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_dispersion_hard_gate"


def apply_hard_gate(
    eligible: pd.DataFrame, daily_dispersion: pd.DataFrame
) -> pd.DataFrame:
    state = daily_dispersion.loc[
        :, ["day", "dispersion", "dispersion_target"]
    ].copy()
    state["gate_open"] = state.dispersion.le(state.dispersion_target)
    allowed = state.set_index("day").gate_open
    result = eligible.copy()
    result["gate_open"] = result.day.map(allowed).fillna(False)
    return result.loc[result.gate_open].copy()


def window_metrics(daily: pd.DataFrame) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, (start, end) in WINDOWS.items():
        result[name] = metrics(daily[daily.day.ge(start) & daily.day.lt(end)])
    result["yearly"] = {
        str(year): metrics(group)
        for year, group in daily.groupby(daily.day.dt.year)
    }
    result["all"] = metrics(daily)
    return result


def terminal_stop_diagnostic(daily: pd.DataFrame, stop: float = 0.30) -> pd.DataFrame:
    """Optimistic screen only; a real stop requires the underlying intraday path."""
    result = daily.copy()
    result["net_return"] = result.net_return.clip(lower=-stop)
    return result


def qualifies(implementations: dict[str, Any]) -> bool:
    for implementation in ("single_position", "single_long_only"):
        stress = implementations[implementation]["stress_0_12pct_one_way"][
            "unprotected"
        ]
        windows_pass = all(
            stress[window]["profit_factor"] > 1.0
            and stress[window]["total_return_pct"] > 0.0
            and stress[window]["max_drawdown_pct"] >= -50.0
            for window in WINDOWS
        )
        years_pass = all(
            item["profit_factor"] > 1.0 and item["total_return_pct"] > 0.0
            for item in stress["yearly"].values()
        )
        if windows_pass and years_pass:
            return True
    return False


def run(data_dirs: Iterable[Path]) -> dict[str, Any]:
    panel = combine_point_in_time_panels(data_dirs)
    eligible = eligible_cross_section(panel)
    dispersion = dispersion_exposure(eligible)
    gated = apply_hard_gate(eligible, dispersion)
    implementations: dict[str, Any] = {}
    for implementation in ("single_position", "single_long_only"):
        implementations[implementation] = {}
        for cost_name, cost in COSTS.items():
            daily = simulate(gated, dispersion, implementation, False, cost)
            implementations[implementation][cost_name] = {
                "unprotected": window_metrics(daily),
                "terminal_stop30_diagnostic": window_metrics(
                    terminal_stop_diagnostic(daily)
                ),
            }
    accepted = qualifies(implementations)
    return {
        "experiment": "s0_dispersion_hard_gate",
        "paper_reference": (
            "Cross-Sectional Dispersion and the State Dependence of "
            "Cryptocurrency Momentum (2026)"
        ),
        "method": (
            "Use the existing point-in-time 20-day demeaned momentum signal, but "
            "trade only when current cross-sectional daily-return dispersion is at "
            "or below its shifted expanding historical median."
        ),
        "diagnostic_warning": (
            "The 30% terminal-return clip is optimistic and cannot qualify a strategy; "
            "it only decides whether an hourly intraday stop replay is worth running."
        ),
        "pre_registered_paths": ["single_position", "single_long_only"],
        "costs": COSTS,
        "coverage": {
            "eligible_rows": int(len(eligible)),
            "gated_rows": int(len(gated)),
            "gate_open_days": int(gated.day.nunique()),
            "all_eligible_days": int(eligible.day.nunique()),
        },
        "implementations": implementations,
        "accepted": accepted,
        "decision": "continue_to_minute_replay" if accepted else "reject_before_shadow_or_live",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit a hard dispersion state gate.")
    parser.add_argument("--data", type=Path, action="append")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    data_dirs = tuple(args.data) if args.data else DEFAULT_DATA
    report = run(data_dirs)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
