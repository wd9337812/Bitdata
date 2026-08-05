from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_s0_adaptive_30d_momentum import (  # noqa: E402
    adjusted_cost,
    apply_event_time_gate,
)
from scripts.benchmark_s0_cross_sectional_momentum import (  # noqa: E402
    Profile,
    simulate_minute,
)

DEFAULT_SIGNALS = (
    ROOT / "data" / "research" / "s0_30d_momentum_adaptive_v2"
    / "signals_all.parquet"
)
DEFAULT_MINUTE = ROOT / "data" / "research" / "binance_um_30d_all_event_1m" / "parquet"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_30d_confluence"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Pre-registered multi-confirmation overlay on 30d momentum: require "
            "2-of-3 among acceleration, breadth and strong 24h momentum."
        )
    )
    parser.add_argument("--signals", type=Path, default=DEFAULT_SIGNALS)
    parser.add_argument("--minute-dir", type=Path, default=DEFAULT_MINUTE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--delay", type=int, default=5)
    parser.add_argument("--cost", type=float, default=0.60)
    parser.add_argument("--min-ret6h-pct", type=float, default=0.5)
    parser.add_argument("--min-breadth-pct", type=float, default=0.5)
    parser.add_argument("--min-ret24h-pct", type=float, default=5.0)
    return parser.parse_args()


def confirmations(signals: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    out = signals.copy()
    out["acceleration"] = (
        np.sign(out.ret_6h).eq(np.sign(out.ret_24h))
        & out.ret_6h.abs().ge(args.min_ret6h_pct / 100.0)
    )
    out["breadth"] = (
        np.sign(out.market_breadth).eq(np.sign(out.direction.map({"LONG": 1, "SHORT": -1})))
        & out.market_breadth.abs().ge(args.min_breadth_pct / 100.0)
    )
    out["strong_momentum"] = out.ret_24h.abs().ge(args.min_ret24h_pct / 100.0)
    out["confirmations"] = (
        out.acceleration.astype(int)
        + out.breadth.astype(int)
        + out.strong_momentum.astype(int)
    )
    return out


FILTERS = (
    ("baseline", None),
    ("need_2", 2),
    ("need_3", 3),
    ("accel_and_breadth", "acceleration&breadth"),
    ("accel_and_strong", "acceleration&strong_momentum"),
    ("breadth_and_strong", "breadth&strong_momentum"),
)


def apply_named_filter(
    signals: pd.DataFrame,
    name: str,
    need: Any,
) -> pd.DataFrame:
    if name == "baseline":
        return signals
    if isinstance(need, int):
        return signals.loc[signals.confirmations.ge(need)].copy()
    fields = need.split("&")
    mask = pd.Series(True, index=signals.index)
    for field in fields:
        mask &= signals[field]
    return signals.loc[mask].copy()


def main() -> None:
    args = parse_args()
    signals = pd.read_parquet(args.signals)
    signals = confirmations(signals, args)
    profile = Profile("frozen_2_5R", ("ret_720h",), 0.05, 2.5, 2.5, 120, 12.0)
    results: dict[str, Any] = {}
    for name, need in FILTERS:
        filtered = apply_named_filter(signals, name, need)
        gross = simulate_minute(
            filtered,
            args.minute_dir,
            profile,
            execution_delay_minutes=args.delay,
            cost_pct=0.0,
            enforce_single_position=False,
        )
        funded = apply_event_time_gate(
            adjusted_cost(gross, args.cost),
            symbol_embargo_hours=72,
        ).copy()
        year = pd.to_datetime(funded.entry_ms, unit="ms", utc=True).dt.year
        split: dict[str, Any] = {}
        for label, mask in [
            ("dev_2021_2023", year.le(2023)),
            ("oos_2024_2026", year.ge(2024)),
        ]:
            sub = funded.loc[mask]
            if sub.empty:
                split[label] = {"trades": 0}
                continue
            pnl = sub.net_pct
            top3 = sub.groupby("symbol").net_pct.sum().nlargest(3).index
            wo = sub.loc[~sub.symbol.isin(top3), "net_pct"]
            wins = pnl.clip(lower=0).sum()
            losses = -pnl.clip(upper=0).sum()
            wo_wins = wo.clip(lower=0).sum()
            wo_losses = -wo.clip(upper=0).sum()
            split[label] = {
                "trades": int(len(sub)),
                "symbols": int(sub.symbol.nunique()),
                "pf": round(float(wins / losses), 3) if losses > 0 else 999.0,
                "net": round(float(pnl.sum()), 2),
                "woTop3_pf": round(float(wo_wins / wo_losses), 3) if wo_losses > 0 else (999.0 if wo_wins > 0 else 0.0),
                "woTop3_net": round(float(wo.sum()), 2),
            }
        results[name] = {
            "signals": int(len(filtered)),
            "confirmations_mean": round(float(filtered.confirmations.mean()), 2),
            **split,
        }
        print(
            f"{name:20s} signals={len(filtered):3d} "
            f"dev={split.get('dev_2021_2023', {}).get('trades', 0):2d} "
            f"PF={split.get('dev_2021_2023', {}).get('pf', 0):.2f} "
            f"wo={split.get('dev_2021_2023', {}).get('woTop3_pf', 0):.2f} | "
            f"oos={split.get('oos_2024_2026', {}).get('trades', 0):2d} "
            f"PF={split.get('oos_2024_2026', {}).get('pf', 0):.2f} "
            f"wo={split.get('oos_2024_2026', {}).get('woTop3_pf', 0):.2f}",
            flush=True,
        )
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(
        json.dumps(
            {
                "experiment": "s0_30d_confluence",
                "filters": [item[0] for item in FILTERS],
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
