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

from scripts.research_s0_phase1_highlev_replay import load_bars  # noqa: E402

DEFAULT_DATA = ROOT / "data" / "research" / "binance_um_1m_cross_year" / "parquet"
DEFAULT_UNIVERSE = ROOT / "data" / "research" / "s0_public_1m" / "universe.json"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_new_listing_first_hour"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Historical pre-registration of the new-listing first-hour momentum "
            "rule: |first-hour return| >=5%, enter at first-hour close, 15% stop, "
            "30% target, 72h cap."
        )
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--universe-json", type=Path, default=DEFAULT_UNIVERSE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--min-first-hour-pct", type=float, default=5.0)
    parser.add_argument("--stop-pct", type=float, default=15.0)
    parser.add_argument("--target-pct", type=float, default=30.0)
    parser.add_argument("--hold-hours", type=int, default=72)
    parser.add_argument("--base-cost-pct", type=float, default=0.12)
    parser.add_argument("--stress-cost-pct", type=float, default=0.24)
    return parser.parse_args()


def simulate_one(
    frame: pd.DataFrame,
    args: argparse.Namespace,
) -> dict[str, Any] | None:
    times = frame.open_time.to_numpy()
    opens = frame.open.to_numpy()
    highs = frame.high.to_numpy()
    lows = frame.low.to_numpy()
    closes = frame.close.to_numpy()
    listing_ms = int(times[0])
    first_hour_end = listing_ms + 3_600_000
    entry_pos = int(np.searchsorted(times, first_hour_end, side="left"))
    if entry_pos <= 0 or entry_pos >= len(times):
        return None
    first_open = float(opens[0])
    first_close = float(closes[entry_pos - 1])
    if first_open <= 0:
        return None
    first_hour_pct = (first_close / first_open - 1.0) * 100.0
    if abs(first_hour_pct) < args.min_first_hour_pct:
        return None
    direction = 1 if first_hour_pct > 0 else -1
    entry = float(opens[entry_pos])
    stop = entry - direction * entry * args.stop_pct / 100.0
    target = entry + direction * entry * args.target_pct / 100.0
    end_pos = min(
        entry_pos + args.hold_hours * 60,
        len(times),
    )
    exit_price = float(closes[end_pos - 1])
    outcome = "TIME"
    for index in range(entry_pos, end_pos):
        if direction > 0 and lows[index] <= stop:
            exit_price, outcome = stop, "STOP"
        elif direction < 0 and highs[index] >= stop:
            exit_price, outcome = stop, "STOP"
        elif direction > 0 and highs[index] >= target:
            exit_price, outcome = target, "TARGET"
        elif direction < 0 and lows[index] <= target:
            exit_price, outcome = target, "TARGET"
        else:
            continue
        break
    gross_pct = direction * (exit_price / entry - 1.0) * 100.0
    return {
        "first_hour_pct": first_hour_pct,
        "direction": direction,
        "gross_pct": gross_pct,
        "outcome": outcome,
    }


def metrics(trades: pd.DataFrame, cost_pct: float) -> dict[str, Any]:
    if trades.empty:
        return {"trades": 0}
    net = trades.gross_pct - cost_pct
    wins = net.clip(lower=0).sum()
    losses = -net.clip(upper=0).sum()
    return {
        "trades": int(len(trades)),
        "win_rate_pct": round(float((net > 0).mean() * 100.0), 2),
        "profit_factor": round(float(wins / losses), 3) if losses > 0 else 999.0,
        "net_sum_pct": round(float(net.sum()), 3),
        "mean_net_pct": round(float(net.mean()), 4),
    }


def main() -> None:
    args = parse_args()
    universe = {
        str(item["symbol"]): item
        for item in json.loads(args.universe_json.read_text(encoding="utf-8"))["symbols"]
    }
    rows: list[dict[str, Any]] = []
    cutoff_2024 = pd.Timestamp("2024-01-01", tz="UTC").timestamp() * 1000
    for path in sorted(args.data.glob("*.parquet")):
        symbol = path.stem
        frame = load_bars(path)
        if frame is None or frame.empty:
            continue
        first = int(frame.open_time.iloc[0])
        if first < cutoff_2024:
            continue
        result = simulate_one(frame, args)
        if result is None:
            continue
        result["symbol"] = symbol
        result["listing_ms"] = first
        rows.append(result)
    trades = pd.DataFrame(rows)
    args.output.mkdir(parents=True, exist_ok=True)
    trades.to_parquet(args.output / "trades.parquet", index=False)
    report: dict[str, Any] = {
        "experiment": "s0_new_listing_first_hour",
        "new_listings_2024plus": 0,
        "qualified": int(len(trades)),
        "annual": {},
        "overall": {},
    }
    if not trades.empty:
        year = pd.to_datetime(trades.listing_ms, unit="ms", utc=True).dt.year
        trades["year"] = year
        for value in sorted(trades.year.unique()):
            scoped = trades.loc[trades.year.eq(value)]
            report["annual"][str(value)] = {
                "base": metrics(scoped, args.base_cost_pct),
                "stress": metrics(scoped, args.stress_cost_pct),
            }
        report["overall"] = {
            "base": metrics(trades, args.base_cost_pct),
            "stress": metrics(trades, args.stress_cost_pct),
        }
        top3 = trades.groupby("symbol").gross_pct.sum().nlargest(3).index
        without = trades.loc[~trades.symbol.isin(top3)]
        report["without_top3"] = {
            "base": metrics(without, args.base_cost_pct),
            "stress": metrics(without, args.stress_cost_pct),
        }
    report["new_listings_2024plus"] = int(
        sum(
            1
            for path in args.data.glob("*.parquet")
            if not load_bars(path).empty
            and int(load_bars(path).open_time.iloc[0]) >= cutoff_2024
        )
    )
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
