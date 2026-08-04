from __future__ import annotations

import argparse
import gc
import json
import sys
from pathlib import Path
from typing import Any, Iterable

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

DEFAULT_DATA = (
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2020_2023",
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2024_2025",
    ROOT / "data" / "research" / "binance_um_point_in_time_1h",
)
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_30d_momentum_adaptive_v2" / "live_config_baseline.json"
STRESS_COST_PCT = 0.60


def simulate_equity_tiered(
    net_pct_points: Iterable[float],
    *,
    start_equity: float = 15.0,
    hard_stop: float = 5.0,
    reference_stop_pct: float = 15.0,
    tiers: tuple[tuple[float, float], ...] = ((0.0, 20.0), (30.0, 22.0)),
) -> dict[str, Any]:
    equity = float(start_equity)
    peak = equity
    max_drawdown = 0.0
    stopped = False
    tier2_trades = 0
    trades = 0
    for value in net_pct_points:
        risk = tiers[0][1]
        for threshold, risk_value in tiers:
            if equity >= threshold:
                risk = risk_value
            else:
                break
        equity *= 1 + float(value) / 100 * risk / reference_stop_pct
        trades += 1
        if risk >= tiers[-1][1]:
            tier2_trades += 1
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak * 100 if peak > 0 else 0)
        if equity <= hard_stop:
            stopped = True
            break
    return {
        "trades": trades,
        "tier2_trades": tier2_trades,
        "final_equity": round(equity, 8),
        "max_drawdown_pct": round(max_drawdown, 6),
        "hard_stop_hit": stopped,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Audit the exact live S0 altcoin configuration: 3.5R, 2.5 ATR stop, "
            "5-day hold, both directions, 20%/22% equity tier."
        )
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    candidate = next(item for item in CANDIDATES if item.name == "momentum_30d_hold_7d")
    profile = adaptive_profile()
    raw_parts: list[pd.DataFrame] = []
    for index, data_dir in enumerate(DEFAULT_DATA, start=1):
        starts, _ = load_manifest(data_dir)
        panel = add_slow_returns(build_panel(data_dir, starts))
        signals = slow_momentum_signals(panel, candidate, minimum_age_days=45)
        signals = signals.loc[
            signals.market_breadth.abs().between(BREADTH_LOW, BREADTH_HIGH)
        ]
        bars = {
            symbol: scoped.sort_values("available_ms").set_index("available_ms")
            for symbol, scoped in panel.groupby("symbol", sort=False)
        }
        raw_parts.append(
            simulate_exit(
                signals,
                bars,
                profile,
                first_r=3.5,
                first_fraction=1.0,
                cost_pct=BASE_COST_PCT,
            )
        )
        print(f"dir{index}: signals={len(signals)}", flush=True)
        del bars, panel, signals
        gc.collect()
    raw = pd.concat(raw_parts, ignore_index=True)
    funded = gate_single_position(raw).assign(
        net_pct=lambda frame: frame.gross_pct - STRESS_COST_PCT
    )
    funded["year"] = pd.to_datetime(funded.entry_ms, unit="ms", utc=True).dt.year
    by_direction = {
        str(direction): {
            "trades": int(len(rows)),
            "profit_factor": round(profit_factor(rows["net_pct"]), 6),
            "net_pct_points": round(float(rows["net_pct"].sum()), 6),
        }
        for direction, rows in funded.groupby("direction")
    }
    report = {
        "experiment": "s0_altcoin_30d_live_config_baseline",
        "live_parameters": {
            "stop_atr": 2.5,
            "reward_r": 3.5,
            "max_hold_hours": 120,
            "max_stop_pct": 12.0,
            "directions": ["LONG", "SHORT"],
            "risk_tier": {"<30U": 20.0, ">=30U": 22.0},
            "cost_pct": STRESS_COST_PCT,
        },
        "trades": int(len(funded)),
        "symbols": int(funded["symbol"].nunique()),
        "profit_factor": round(profit_factor(funded["net_pct"]), 6),
        "net_pct_points": round(float(funded["net_pct"].sum()), 6),
        "by_direction": by_direction,
        "equity": simulate_equity_tiered(funded["net_pct"]),
        "by_year": {
            str(int(year)): simulate_equity_tiered(rows["net_pct"])
            for year, rows in funded.groupby("year")
        },
        "warning": (
            "Historical replay with the exact live parameters; it is not a "
            "promise of future returns."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
