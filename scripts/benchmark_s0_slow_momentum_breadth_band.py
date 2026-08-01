from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_s0_xmom_regime_candidates import block_bootstrap, scope  # noqa: E402
from scripts.benchmark_s0_cross_sectional_momentum import simulate_minute, summarize  # noqa: E402
from scripts.benchmark_s0_point_in_time_slow_momentum import (  # noqa: E402
    CANDIDATES,
    profile_for,
)


DEFAULT_RESEARCH = ROOT / "data" / "research" / "s0_point_in_time_slow_momentum"
DEFAULT_MINUTE_DIR = ROOT / "data" / "research" / "binance_um_event_1m" / "parquet"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_slow_momentum_breadth_band"
BASE_COST = 0.24
STRESS_COST = 0.36


def cohort(symbol: str) -> str:
    bucket = int(hashlib.sha256(symbol.encode("utf-8")).hexdigest()[:8], 16) % 10
    return "research" if bucket < 6 else "validation" if bucket < 8 else "blind"


def _windows(frame: pd.DataFrame) -> dict[str, dict[str, Any]]:
    return {
        name: summarize(scope(frame, start, end))
        for name, start, end in (
            ("development", "2026-02-01", "2026-04-01"),
            ("validation_apr_may", "2026-04-01", "2026-06-01"),
            ("test_june", "2026-06-01", "2026-07-01"),
            ("final_july", "2026-07-01", "2026-08-01"),
        )
    }


def report(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {"overall": summarize(frame), "windows": _windows(frame), "cohorts": {}}
    frame = frame.copy()
    frame["cohort"] = frame.symbol.map(cohort)
    cohorts = {name: summarize(group) for name, group in frame.groupby("cohort", sort=True)}
    top_symbols = frame.groupby("symbol").net_pct.sum().nlargest(3).index
    return {
        "overall": summarize(frame),
        "windows": _windows(frame),
        "cohorts": cohorts,
        "without_top_3_symbols": summarize(frame.loc[~frame.symbol.isin(top_symbols)]),
        "bootstrap": block_bootstrap(frame),
    }


def qualifies(reports: dict[str, dict[str, Any]]) -> bool:
    base = reports["base"]
    overall = base["overall"]
    if overall.get("trades", 0) < 40 or overall.get("symbols", 0) < 12:
        return False
    for window in ("validation_apr_may", "test_june", "final_july"):
        metrics = base["windows"][window]
        if metrics.get("profit_factor", 0) <= 1 or metrics.get("net_pct_points", 0) <= 0:
            return False
    blind = base["cohorts"].get("blind", {})
    if blind.get("trades", 0) < 12 or blind.get("net_pct_points", 0) <= 0:
        return False
    if base["without_top_3_symbols"].get("net_pct_points", 0) <= 0:
        return False
    return reports["stress"]["overall"].get("net_pct_points", 0) > 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark frozen slow-momentum breadth band.")
    parser.add_argument("--research", type=Path, default=DEFAULT_RESEARCH)
    parser.add_argument("--minute-dir", type=Path, default=DEFAULT_MINUTE_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    signals = pd.read_parquet(args.research / "selected_signals.parquet")
    signals = signals.loc[signals.market_breadth.abs().between(0.015, 0.075)].copy()
    candidate = next(item for item in CANDIDATES if item.name == "momentum_7d_hold_1d")
    profile = profile_for(candidate)
    base = simulate_minute(signals, args.minute_dir, profile, execution_delay_minutes=5, cost_pct=BASE_COST)
    stress = simulate_minute(signals, args.minute_dir, profile, execution_delay_minutes=5, cost_pct=STRESS_COST)
    reports = {"base": report(base), "stress": report(stress)}
    base.to_parquet(args.output / "trades_base.parquet", index=False)
    stress.to_parquet(args.output / "trades_stress.parquet", index=False)
    result = {
        "experiment": "s0_slow_momentum_breadth_band",
        "rule": "momentum_7d_hold_1d with 0.015 <= abs(market_breadth) <= 0.075",
        "signal_count": int(len(signals)),
        "reports": reports,
        "qualified_for_research_shadow": qualifies(reports),
        "decision": "qualified_for_research_shadow" if qualifies(reports) else "research_only_not_eligible",
    }
    (args.output / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
