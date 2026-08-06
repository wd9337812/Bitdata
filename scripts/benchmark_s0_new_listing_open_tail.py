from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_DATA = ROOT / "data" / "research" / "binance_um_1m_cross_year" / "parquet"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_new_listing_open_tail"
CUTOFF_2024_MS = int(pd.Timestamp("2024-01-01", tz="UTC").timestamp() * 1000)
HOLD_HOURS = 72
MINUTE_MS = 60_000


@dataclass(frozen=True)
class Structure:
    name: str
    entry_minutes: int
    stop_pct: float
    target_pct: float | None
    trail_trigger_pct: float | None
    trail_dist_pct: float | None


STRUCTURES = (
    Structure("open_s15_tp33", 0, 15.0, 33.0, None, None),
    Structure("open_s15_tp50", 0, 15.0, 50.0, None, None),
    Structure("open_s15_tp100", 0, 15.0, 100.0, None, None),
    Structure("open_s10_tp33", 0, 10.0, 33.0, None, None),
    Structure("open_s20_tp50", 0, 20.0, 50.0, None, None),
    Structure("h1_s15_tp33", 60, 15.0, 33.0, None, None),
    Structure("h1_s15_tp50", 60, 15.0, 50.0, None, None),
    Structure("open_s20_trail20d15", 0, 20.0, None, 20.0, 15.0),
    Structure("open_s15_run72", 0, 15.0, None, None, None),
    Structure("h1_s15_run72", 60, 15.0, None, None, None),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Pre-registered new-listing open-tail family: enter at listing "
            "open (or first hour), bounded stop, fixed/trailing target, 72h cap. "
            "2024 selection / 2025 validation / 2026 blind, top-3 removed, "
            "symbol-block bootstrap and 10U account path."
        )
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--base-cost-pct", type=float, default=0.24)
    parser.add_argument("--stress-cost-pct", type=float, default=0.60)
    parser.add_argument("--slippage-pct", type=float, default=0.0)
    parser.add_argument("--initial-equity", type=float, default=10.0)
    parser.add_argument("--hard-stop-equity", type=float, default=5.0)
    parser.add_argument("--position-fraction", type=float, default=1.0)
    parser.add_argument("--bootstrap-samples", type=int, default=1_000)
    parser.add_argument("--bootstrap-seed", type=int, default=20260805)
    parser.add_argument("--structures", nargs="*", default=None)
    return parser.parse_args()


def listing_frames(data: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(data.glob("*.parquet")):
        pf = pq.ParquetFile(path)
        first_ms = int(pf.metadata.row_group(0).column(0).statistics.min)
        if first_ms < CUTOFF_2024_MS:
            continue
        end_ms = first_ms + HOLD_HOURS * 3_600_000
        table = pq.read_table(
            path,
            columns=["open_time", "open", "high", "low", "close"],
            filters=[("open_time", ">=", first_ms), ("open_time", "<", end_ms)],
        )
        frame = table.to_pandas().sort_values("open_time").reset_index(drop=True)
        if len(frame) < 60:
            continue
        rows.append({"symbol": path.stem, "listing_ms": first_ms, "frame": frame})
    return rows


def simulate_one(
    frame: pd.DataFrame,
    structure: Structure,
    slippage_pct: float,
) -> dict[str, Any] | None:
    times = frame.open_time.to_numpy()
    opens = frame.open.to_numpy()
    highs = frame.high.to_numpy()
    lows = frame.low.to_numpy()
    closes = frame.close.to_numpy()
    entry_idx = structure.entry_minutes
    if entry_idx >= len(times):
        return None
    entry = float(opens[entry_idx]) * (1.0 + slippage_pct / 100.0)
    if entry <= 0:
        return None
    stop = entry * (1.0 - structure.stop_pct / 100.0)
    target = (
        entry * (1.0 + structure.target_pct / 100.0)
        if structure.target_pct is not None
        else None
    )
    trail_dist = (
        entry * structure.trail_dist_pct / 100.0
        if structure.trail_dist_pct is not None
        else None
    )
    trigger = (
        entry * (1.0 + structure.trail_trigger_pct / 100.0)
        if structure.trail_trigger_pct is not None
        else None
    )
    armed = False
    best = entry
    trail_stop: float | None = None
    end = min(entry_idx + HOLD_HOURS * 60, len(times))
    for index in range(entry_idx, end):
        high = float(highs[index])
        low = float(lows[index])
        if low <= stop:
            return {
                "exit": stop,
                "outcome": "STOP",
                "hold_minutes": index - entry_idx + 1,
            }
        if target is not None and high >= target:
            return {
                "exit": target,
                "outcome": "TARGET",
                "hold_minutes": index - entry_idx + 1,
            }
        if trigger is not None:
            if not armed and high >= trigger:
                armed = True
                best = max(best, high)
                trail_stop = max(best - trail_dist, stop)
            elif armed:
                best = max(best, high)
                trail_stop = max(trail_stop, best - trail_dist)
                if low <= trail_stop:
                    return {
                        "exit": trail_stop,
                        "outcome": "TRAIL",
                        "hold_minutes": index - entry_idx + 1,
                    }
    return {
        "exit": float(closes[end - 1]),
        "outcome": "TIME",
        "hold_minutes": end - entry_idx,
    }


def simulate_structure(
    listings: list[dict[str, Any]],
    structure: Structure,
    cost_pct: float,
    slippage_pct: float,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for item in listings:
        result = simulate_one(item["frame"], structure, slippage_pct)
        if result is None:
            continue
        entry_price = float(item["frame"].open.iloc[structure.entry_minutes]) * (
            1.0 + slippage_pct / 100.0
        )
        gross_pct = (result["exit"] / entry_price - 1.0) * 100.0
        rows.append(
            {
                "symbol": item["symbol"],
                "listing_ms": item["listing_ms"],
                "year": pd.Timestamp(
                    item["listing_ms"], unit="ms", tz="UTC"
                ).year,
                "outcome": result["outcome"],
                "hold_minutes": result["hold_minutes"],
                "exit_ms": item["listing_ms"]
                + (structure.entry_minutes + result["hold_minutes"]) * MINUTE_MS,
                "gross_pct": gross_pct,
                "net_pct": gross_pct - cost_pct,
            }
        )
    return pd.DataFrame(rows)


def metrics(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {"trades": 0}
    net = frame.net_pct
    wins = net.clip(lower=0).sum()
    losses = -net.clip(upper=0).sum()
    return {
        "trades": int(len(frame)),
        "win_rate_pct": round(float((net > 0).mean() * 100.0), 2),
        "profit_factor": round(
            float(wins / losses) if losses > 0 else (999.0 if wins > 0 else 0.0),
            3,
        ),
        "net_sum_pct": round(float(net.sum()), 2),
        "mean_net_pct": round(float(net.mean()), 4),
        "stop_count": int((frame.outcome == "STOP").sum()),
        "target_count": int((frame.outcome == "TARGET").sum()),
        "trail_count": int((frame.outcome == "TRAIL").sum()),
        "time_count": int((frame.outcome == "TIME").sum()),
    }


def split_report(
    frame: pd.DataFrame,
) -> dict[str, Any]:
    if frame.empty:
        return {}
    report: dict[str, Any] = {"all": metrics(frame)}
    for year in sorted(frame.year.unique()):
        report[str(year)] = metrics(frame.loc[frame.year.eq(year)])
    top3 = frame.groupby("symbol").net_pct.sum().nlargest(3).index
    report["without_top3"] = metrics(frame.loc[~frame.symbol.isin(top3)])
    return report


def bootstrap_symbols(
    frame: pd.DataFrame,
    samples: int,
    seed: int,
) -> dict[str, Any]:
    if frame.empty:
        return {"samples": 0}
    symbols = sorted(frame.symbol.unique())
    blocks = {
        symbol: frame.loc[frame.symbol.eq(symbol)].net_pct.to_numpy(dtype=float)
        for symbol in symbols
    }
    rng = np.random.default_rng(seed)
    totals = np.empty(samples, dtype=float)
    for index in range(samples):
        chosen = rng.integers(0, len(symbols), size=len(symbols))
        totals[index] = sum(float(blocks[symbols[item]].sum()) for item in chosen)
    return {
        "samples": samples,
        "symbol_blocks": len(symbols),
        "positive_probability": round(float((totals > 0).mean()), 4),
        "net_pct_p05": round(float(np.quantile(totals, 0.05)), 4),
        "net_pct_p50": round(float(np.quantile(totals, 0.50)), 4),
        "net_pct_p95": round(float(np.quantile(totals, 0.95)), 4),
    }


def simulate_account(
    frame: pd.DataFrame,
    args: argparse.Namespace,
) -> dict[str, Any]:
    if frame.empty:
        return {"trades": 0}
    ordered = frame.sort_values("listing_ms").reset_index(drop=True)
    equity = float(args.initial_equity)
    hard_stop = float(args.hard_stop_equity)
    fraction = float(args.position_fraction)
    curve: list[dict[str, Any]] = [{"time": 0, "equity": equity, "symbol": ""}]
    busy_until = -1
    for row in ordered.itertuples(index=False):
        entry_ms = int(row.listing_ms) + 0
        if entry_ms < busy_until:
            continue
        pnl_equity_pct = fraction * float(row.net_pct)
        equity *= 1.0 + pnl_equity_pct / 100.0
        busy_until = int(row.exit_ms)
        curve.append(
            {
                "time": busy_until,
                "equity": round(equity, 8),
                "symbol": row.symbol,
            }
        )
        if equity <= hard_stop:
            break
    equity_arr = np.array([item["equity"] for item in curve], dtype=float)
    peak = np.maximum.accumulate(equity_arr)
    drawdown = (peak - equity_arr) / peak
    return {
        "trades": int(len(curve) - 1),
        "final_equity": round(float(equity), 8),
        "multiplier": round(float(equity) / float(args.initial_equity), 4),
        "max_drawdown_pct": round(float(drawdown.max() * 100.0), 4),
        "ruined": bool(equity <= hard_stop),
        "reached_10000u": bool(equity >= 10_000.0),
    }


def bootstrap_account(
    frame: pd.DataFrame,
    args: argparse.Namespace,
) -> dict[str, Any]:
    if frame.empty:
        return {"samples": 0}
    symbols = sorted(frame.symbol.unique())
    blocks = {
        symbol: frame.loc[frame.symbol.eq(symbol)].reset_index(drop=True)
        for symbol in symbols
    }
    rng = np.random.default_rng(args.bootstrap_seed)
    finals = np.empty(args.bootstrap_samples, dtype=float)
    reached = np.zeros(args.bootstrap_samples, dtype=bool)
    ruined = np.zeros(args.bootstrap_samples, dtype=bool)
    for sample in range(args.bootstrap_samples):
        chosen = rng.integers(0, len(symbols), size=len(symbols))
        sample_rows = pd.concat(
            [blocks[symbols[index]] for index in chosen],
            ignore_index=True,
        ).sort_values("listing_ms").reset_index(drop=True)
        equity = float(args.initial_equity)
        fraction = float(args.position_fraction)
        busy_until = -1
        for row in sample_rows.itertuples(index=False):
            entry_ms = int(row.listing_ms)
            if entry_ms < busy_until:
                continue
            equity *= 1.0 + fraction * float(row.net_pct) / 100.0
            busy_until = int(row.exit_ms)
            if equity <= float(args.hard_stop_equity):
                break
        finals[sample] = equity
        reached[sample] = equity >= 10_000.0
        ruined[sample] = equity <= float(args.hard_stop_equity)
    return {
        "samples": int(args.bootstrap_samples),
        "symbol_blocks": len(symbols),
        "p10_equity": round(float(np.quantile(finals, 0.10)), 4),
        "p50_equity": round(float(np.quantile(finals, 0.50)), 4),
        "p90_equity": round(float(np.quantile(finals, 0.90)), 4),
        "ruin_probability": round(float(ruined.mean()), 4),
        "reached_10000u_probability": round(float(reached.mean()), 4),
    }


def main() -> None:
    args = parse_args()
    listings = listing_frames(args.data)
    structures = [
        structure
        for structure in STRUCTURES
        if args.structures is None or structure.name in args.structures
    ]
    report: dict[str, Any] = {
        "experiment": "s0_new_listing_open_tail",
        "listings_2024plus": len(listings),
        "cost": {"base": args.base_cost_pct, "stress": args.stress_cost_pct},
        "slippage_pct": args.slippage_pct,
        "structures": {},
    }
    for structure in structures:
        stress = simulate_structure(
            listings, structure, args.stress_cost_pct, args.slippage_pct
        )
        base = simulate_structure(
            listings, structure, args.base_cost_pct, args.slippage_pct
        )
        entry = {
            "stress": split_report(stress),
            "base": split_report(base),
            "bootstrap_stress": bootstrap_symbols(
                stress, args.bootstrap_samples, args.bootstrap_seed
            ),
            "account_pos1x": simulate_account(stress, args),
            "bootstrap_account_pos1x": bootstrap_account(stress, args),
        }
        report["structures"][structure.name] = entry
        stress_all = entry["stress"].get("all", {})
        wo = entry["stress"].get("without_top3", {})
        print(
            f"{structure.name:20s} n={stress_all.get('trades', 0):3d} "
            f"win={stress_all.get('win_rate_pct', 0):5.1f}% "
            f"PF={stress_all.get('profit_factor', 0):5.2f} "
            f"woPF={wo.get('profit_factor', 0):5.2f} "
            f"2025PF={entry['stress'].get('2025', {}).get('profit_factor', 0):5.2f} "
            f"acc={entry['account_pos1x'].get('final_equity', 0):8.2f} "
            f"10000u={entry['bootstrap_account_pos1x'].get('reached_10000u_probability', 0)}",
            flush=True,
        )
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
