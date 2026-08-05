from __future__ import annotations

import argparse
import gc
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_s0_funding_crowding_reversal import (  # noqa: E402
    BASE_ONE_WAY_COST,
    DATA_DIRS,
    FUNDING_DIRS,
    STRESS_ONE_WAY_COST,
    WINDOWS,
    load_funding,
    merge_funding,
    metrics,
    simulate,
)
from scripts.benchmark_s0_xmom_point_in_time import (  # noqa: E402
    build_panel,
    load_manifest,
)

DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_funding_exhaustion"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Funding-extreme + momentum-exhaustion reversal: |funding|>=0.10%, "
            "24h move >=2% then 6h stall; fade at next hour open; same exits as "
            "the rejected baseline (10% stop / 2R / 24h) to isolate the filter."
        )
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--min-abs-funding-pct", type=float, default=0.10)
    parser.add_argument("--min-24h-move-pct", type=float, default=2.0)
    parser.add_argument("--max-6h-move-pct", type=float, default=0.30)
    parser.add_argument("--min-liquidity-24h", type=float, default=20_000_000.0)
    parser.add_argument("--min-age-days", type=int, default=45)
    return parser.parse_args()


def signals_with_exhaustion(
    panel: pd.DataFrame,
    args: argparse.Namespace,
) -> pd.DataFrame:
    eligible = panel.loc[
        panel.symbol_age_days.ge(args.min_age_days)
        & panel.liquidity_24h.ge(args.min_liquidity_24h)
        & panel.funding_rate_pct.abs().ge(args.min_abs_funding_pct)
        & panel.funding_age_hours.between(0, 12)
        & panel.ret_24h.abs().ge(args.min_24h_move_pct / 100.0)
    ].copy()
    if eligible.empty:
        return eligible
    stalled = (
        eligible.ret_6h.abs().le(args.max_6h_move_pct / 100.0)
        | (np.sign(eligible.ret_6h) != np.sign(eligible.ret_24h))
    )
    eligible = eligible.loc[stalled].copy()
    if eligible.empty:
        return eligible
    eligible["direction"] = np.where(
        eligible.funding_rate_pct.gt(0),
        "SHORT",
        "LONG",
    )
    eligible["abs_funding_pct"] = eligible.funding_rate_pct.abs()
    eligible["strength"] = eligible.groupby(
        "available_ms",
        sort=False,
    )["abs_funding_pct"].rank(pct=True)
    return (
        eligible.sort_values(
            ["available_ms", "abs_funding_pct", "liquidity_24h"],
            ascending=[True, False, False],
        )
        .groupby("available_ms", sort=False)
        .head(1)
        .sort_values("available_ms")
        .reset_index(drop=True)
    )


def main() -> None:
    args = parse_args()
    funding = load_funding(FUNDING_DIRS)
    panel_parts: list[pd.DataFrame] = []
    for data_dir in DATA_DIRS:
        starts, _ = load_manifest(data_dir)
        panel = build_panel(data_dir, starts)
        panel = panel.loc[panel.symbol.isin(funding)]
        panel_parts.append(panel)
        del panel
        gc.collect()
    panel = pd.concat(panel_parts, ignore_index=True).reset_index(drop=True)
    panel = merge_funding(panel, funding)
    signals = signals_with_exhaustion(panel, args)
    from scripts.audit_s0_funding_crowding_reversal import Profile as FundingProfile

    profile = FundingProfile(
        name="fund_exhaustion",
        min_abs_funding_pct=args.min_abs_funding_pct,
        hold_hours=24,
        stop_pct=0.10,
        take_r=2.0,
    )
    base = simulate(signals, panel, profile, BASE_ONE_WAY_COST)
    stress = simulate(signals, panel, profile, STRESS_ONE_WAY_COST)
    for frame in (base, stress):
        frame["year"] = pd.to_datetime(frame.entry_ms, unit="ms", utc=True).dt.year
    report: dict[str, Any] = {
        "experiment": "s0_funding_exhaustion",
        "signals": int(len(signals)),
        "trades": int(len(base)),
        "windows": {},
        "annual": {},
    }
    for name, (start, end) in WINDOWS.items():
        mask = base.year.ge(pd.to_datetime(start).year) & base.year.lt(
            pd.to_datetime(end).year
        )
        report["windows"][name] = {
            "base": metrics(base.loc[mask]),
            "stress": metrics(stress.loc[mask]),
        }
    for year in sorted(base.year.unique()):
        report["annual"][str(year)] = metrics(stress.loc[stress.year.eq(year)])
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
