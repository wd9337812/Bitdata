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

from scripts.benchmark_s0_point_in_time_slow_momentum import (  # noqa: E402
    CANDIDATES,
    add_slow_returns,
    slow_momentum_signals,
)
from scripts.benchmark_s0_xmom_point_in_time import (  # noqa: E402
    build_panel,
    load_manifest,
)
from scripts.benchmark_s0_adaptive_30d_momentum import (  # noqa: E402
    BREADTH_HIGH,
    BREADTH_LOW,
    adaptive_profile,
)
from scripts.benchmark_s0_altcoin_30d_exit_profiles import (  # noqa: E402
    BASE_COST_PCT,
    gate_single_position,
    profit_factor,
    simulate_exit,
)
from scripts.audit_s0_altcoin_30d_live_config import (  # noqa: E402
    STRESS_COST_PCT,
    simulate_equity_tiered,
)

DEFAULT_DATA = (
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2020_2023",
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2024_2025",
    ROOT / "data" / "research" / "binance_um_point_in_time_1h",
)
DEFAULT_FUNDING = ROOT / "data" / "research" / "binance_um_point_in_time_funding"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_30d_momentum_adaptive_v2" / "funding_filter_audit.json"
FUNDING_THRESHOLDS = (0.0, 0.01, 0.03, 0.05, 0.10)


def load_funding(funding_dir: Path) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for path in sorted(funding_dir.glob("*-funding.parquet")):
        symbol = path.name.split("-funding.parquet")[0].upper()
        frame = pd.read_parquet(path)
        if frame.empty:
            continue
        frame = frame.sort_values("timestamp_ms").copy()
        frame["funding_available_ms"] = frame.timestamp_ms.astype("int64") + 60_000
        frames[symbol] = frame[["funding_available_ms", "last_funding_rate"]]
    return frames


def merge_funding(
    signals: pd.DataFrame,
    funding: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for symbol, frame in signals.groupby("symbol", sort=False):
        funding_frame = funding.get(symbol.upper())
        if funding_frame is None or funding_frame.empty:
            parts.append(frame.assign(funding_rate_pct=float("nan")))
            continue
        merged = pd.merge_asof(
            frame.sort_values("available_ms"),
            funding_frame.sort_values("funding_available_ms"),
            left_on="available_ms",
            right_on="funding_available_ms",
            direction="backward",
        )
        merged["funding_rate_pct"] = (
            pd.to_numeric(merged.last_funding_rate, errors="coerce") * 100
        )
        parts.append(merged.drop(columns=["funding_available_ms", "last_funding_rate"]))
    return pd.concat(parts, ignore_index=True).sort_values("available_ms").reset_index(drop=True)


def apply_funding_filter(
    signals: pd.DataFrame,
    threshold_pct: float,
) -> pd.DataFrame:
    if threshold_pct <= 0:
        return signals
    long_ok = signals.direction.eq("LONG") & signals.funding_rate_pct.le(threshold_pct)
    short_ok = signals.direction.eq("SHORT") & signals.funding_rate_pct.ge(-threshold_pct)
    missing = signals.funding_rate_pct.isna()
    return signals.loc[long_ok | short_ok | missing].copy()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit a funding-rate filter on the 30-day alt momentum route."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    candidate = next(item for item in CANDIDATES if item.name == "momentum_30d_hold_7d")
    profile = adaptive_profile()
    funding = load_funding(DEFAULT_FUNDING)
    signal_parts: list[pd.DataFrame] = []
    bar_parts: dict[str, list[pd.DataFrame]] = {name: [] for name in ("no_filter",)}
    for name in FUNDING_THRESHOLDS:
        bar_parts[f"thr_{name:g}"] = []
    for index, data_dir in enumerate(DEFAULT_DATA, start=1):
        starts, _ = load_manifest(data_dir)
        panel = add_slow_returns(build_panel(data_dir, starts))
        signals = slow_momentum_signals(panel, candidate, minimum_age_days=45)
        signals = signals.loc[
            signals.market_breadth.abs().between(BREADTH_LOW, BREADTH_HIGH)
        ]
        signals = merge_funding(signals, funding)
        bars = {
            symbol: scoped.sort_values("available_ms").set_index("available_ms")
            for symbol, scoped in panel.groupby("symbol", sort=False)
        }
        for threshold in FUNDING_THRESHOLDS:
            filtered = apply_funding_filter(signals, threshold)
            bar_parts[f"thr_{threshold:g}"].append(
                simulate_exit(
                    filtered,
                    bars,
                    profile,
                    first_r=3.5,
                    first_fraction=1.0,
                    cost_pct=BASE_COST_PCT,
                )
            )
        print(f"dir{index}: signals={len(signals)} funding_covered={signals.funding_rate_pct.notna().sum()}", flush=True)
        del bars, panel, signals
        gc.collect()

    reports: dict[str, Any] = {}
    for threshold in FUNDING_THRESHOLDS:
        raw = pd.concat(bar_parts[f"thr_{threshold:g}"], ignore_index=True)
        funded = gate_single_position(raw).assign(
            net_pct=lambda frame: frame.gross_pct - STRESS_COST_PCT
        )
        funded["year"] = pd.to_datetime(funded.entry_ms, unit="ms", utc=True).dt.year
        reports[f"thr_{threshold:g}"] = {
            "threshold_pct": threshold,
            "trades": int(len(funded)),
            "symbols": int(funded["symbol"].nunique()),
            "profit_factor": round(profit_factor(funded["net_pct"]), 6),
            "net_pct_points": round(float(funded["net_pct"].sum()), 6),
            "equity": simulate_equity_tiered(funded["net_pct"]),
            "by_year": {
                str(int(year)): simulate_equity_tiered(rows["net_pct"])
                for year, rows in funded.groupby("year")
            },
        }
        e = reports[f"thr_{threshold:g}"]["equity"]
        print(
            f"thr_{threshold:g}: trades={reports[f'thr_{threshold:g}']['trades']} "
            f"PF={reports[f'thr_{threshold:g}']['profit_factor']} "
            f"final={e['final_equity']}U hard={e['hard_stop_hit']}",
            flush=True,
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "experiment": "s0_altcoin_30d_funding_filter",
                "thresholds_pct": list(FUNDING_THRESHOLDS),
                "reports": reports,
                "warning": "Historical qualification is not live-trading approval.",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
