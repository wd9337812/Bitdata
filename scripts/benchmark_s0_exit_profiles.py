from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = ROOT / "data" / "research" / "s0_public_1m" / "s0_candidates_1m.parquet"
MINUTE_DIR = ROOT / "data" / "research" / "s0_public_1m" / "parquet"
OUTPUT = ROOT / "data" / "research" / "s0_exit_profile_benchmark.json"
MINUTE_MS = 60_000
SIGNAL_MINUTES = 5
ROUND_TRIP_COST_PCT = 0.12
SOURCE_STOP_ATR = 0.85

PROFILES = {
    "current_fast": {"stop_atr": 0.85, "take_profit_r": 1.05, "hold_minutes": 10},
    "tight_fast": {"stop_atr": 0.65, "take_profit_r": 1.30, "hold_minutes": 8},
    "balanced_20m": {"stop_atr": 0.90, "take_profit_r": 1.50, "hold_minutes": 20},
    "trend_30m": {"stop_atr": 1.00, "take_profit_r": 1.80, "hold_minutes": 30},
    "trend_60m": {"stop_atr": 1.20, "take_profit_r": 2.00, "hold_minutes": 60},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark fixed S0 exit profiles on raw 1m paths.")
    parser.add_argument("--candidates", type=Path, default=CANDIDATES)
    parser.add_argument("--minute-dir", type=Path, default=MINUTE_DIR)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    return parser.parse_args()


def _exact_windows(times: np.ndarray, starts: np.ndarray, width: int) -> tuple[np.ndarray, np.ndarray]:
    positions = np.searchsorted(times, starts)
    offsets = np.arange(width, dtype=np.int64)
    matrix = positions[:, None] + offsets[None, :]
    in_bounds = matrix[:, -1] < len(times)
    clipped = np.minimum(matrix, max(len(times) - 1, 0))
    expected = starts[:, None] + offsets[None, :] * MINUTE_MS
    exact = in_bounds & np.all(times[clipped] == expected, axis=1)
    return clipped, exact


def relabel_profile(
    minute: pd.DataFrame,
    candidates: pd.DataFrame,
    *,
    stop_atr: float,
    take_profit_r: float,
    hold_minutes: int,
) -> pd.DataFrame:
    times = minute.open_time.to_numpy(dtype=np.int64)
    starts = candidates.open_time.to_numpy(dtype=np.int64) + SIGNAL_MINUTES * MINUTE_MS
    indices, valid = _exact_windows(times, starts, hold_minutes)
    open_ = minute.open.to_numpy(dtype=float)[indices]
    high = minute.high.to_numpy(dtype=float)[indices]
    low = minute.low.to_numpy(dtype=float)[indices]
    close = minute.close.to_numpy(dtype=float)[indices]
    entry = open_[:, 0]
    source_entry = candidates.entry.to_numpy(dtype=float)
    source_stop = candidates.stop.to_numpy(dtype=float)
    atr = np.abs(source_entry - source_stop) / SOURCE_STOP_ATR
    long_side = candidates.direction.to_numpy() == "LONG"
    sign = np.where(long_side, 1.0, -1.0)
    stop = entry - sign * atr * stop_atr
    take = entry + sign * atr * stop_atr * take_profit_r
    exit_price = close[:, -1].copy()
    exit_minute = np.full(len(candidates), hold_minutes, dtype=np.int16)
    outcome = np.full(len(candidates), "TIME_EXIT", dtype=object)
    active = valid.copy()
    for offset in range(hold_minutes):
        hit_stop = np.where(long_side, low[:, offset] <= stop, high[:, offset] >= stop)
        hit_take = np.where(long_side, high[:, offset] >= take, low[:, offset] <= take)
        stop_now = active & hit_stop
        exit_price[stop_now] = stop[stop_now]
        exit_minute[stop_now] = offset + 1
        outcome[stop_now] = "STOP"
        active[stop_now] = False
        take_now = active & hit_take
        exit_price[take_now] = take[take_now]
        exit_minute[take_now] = offset + 1
        outcome[take_now] = "TAKE_PROFIT"
        active[take_now] = False
    gross = sign * (np.divide(exit_price, entry, out=np.ones_like(exit_price), where=entry != 0) - 1) * 100
    net = gross - ROUND_TRIP_COST_PCT
    net[~valid] = np.nan
    outcome[~valid] = "MISSING_PATH"
    exit_minute[~valid] = 0
    return pd.DataFrame(
        {
            "net_pct": net,
            "exit_minute": exit_minute,
            "outcome": outcome,
            "valid": valid,
        },
        index=candidates.index,
    )


def _prepare_candidates(path: Path) -> pd.DataFrame:
    columns = [
        "symbol",
        "open_time",
        "direction",
        "setup_type",
        "entry",
        "stop",
        "market_regime",
        "rank_percentile",
        "quality",
        "expected_net_pct",
        "lower_expected_net_pct",
        "confirmations",
        "baseline_passed",
        "medium_alignment",
        "regime_fit",
        "directed_flow",
        "volume_persistence",
        "anti_chase",
        "entry_quality",
    ]
    frame = pd.read_parquet(path, columns=columns)
    numeric = [
        "rank_percentile",
        "quality",
        "expected_net_pct",
        "lower_expected_net_pct",
        "confirmations",
        "medium_alignment",
        "regime_fit",
        "directed_flow",
        "volume_persistence",
        "anti_chase",
        "entry_quality",
    ]
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
    frame["time"] = pd.to_datetime(frame.open_time, unit="ms", utc=True) + pd.Timedelta(
        minutes=SIGNAL_MINUTES
    )
    frame["batch"] = (frame.open_time.astype("int64") // (5 * MINUTE_MS)).astype("int64")
    frame["score"] = (
        frame.rank_percentile * 0.28
        + frame.quality * 0.22
        + frame.regime_fit * 0.12
        + frame.directed_flow * 0.10
        + frame.volume_persistence * 0.08
        + frame.anti_chase * 0.08
        + frame.entry_quality * 0.12
    )
    aligned = (
        (frame.market_regime.eq("broad_up") & frame.direction.eq("LONG"))
        | (frame.market_regime.eq("broad_down") & frame.direction.eq("SHORT"))
        | (frame.market_regime.isin(["quiet", "rotation"]) & frame.medium_alignment.ge(0.5))
    )
    frame["v53_proxy"] = (
        aligned
        & frame.rank_percentile.ge(0.85)
        & frame.quality.ge(0.56)
        & frame.expected_net_pct.ge(0.03)
        & frame.lower_expected_net_pct.ge(-0.03)
        & frame.confirmations.ge(2)
    )
    frame["rank95"] = frame.rank_percentile.ge(0.95)
    useful = (
        frame.baseline_passed.fillna(False)
        | frame.v53_proxy
        | frame.rank95
    )
    return frame[useful].reset_index(drop=True).copy()


def _schedule(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    frame = frame.reset_index(drop=True)
    ranked = (
        frame.sort_values(["batch", "score"], ascending=[True, False])
        .drop_duplicates("batch")
        .sort_values("time")
    )
    selected: list[int] = []
    free_at: pd.Timestamp | None = None
    symbol_free: dict[tuple[str, str], pd.Timestamp] = {}
    for row in ranked.itertuples():
        key = (str(row.symbol), str(row.direction))
        if free_at is not None and row.time < free_at:
            continue
        if row.time < symbol_free.get(key, pd.Timestamp("1970-01-01", tz="UTC")):
            continue
        selected.append(row.Index)
        free_at = row.time + pd.Timedelta(minutes=max(int(row.exit_minute), 1))
        symbol_free[key] = row.time + pd.Timedelta(minutes=30)
    return ranked.loc[selected].sort_values("time").reset_index(drop=True)


def _metrics(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {"trades": 0, "net_pct_points": 0.0}
    values = frame.net_pct.fillna(0.0)
    gains = float(values[values > 0].sum())
    losses = float(-values[values < 0].sum())
    curve = values.cumsum()
    drawdown = curve.cummax() - curve
    return {
        "trades": int(len(frame)),
        "symbols": int(frame.symbol.nunique()),
        "regimes": int(frame.market_regime.nunique()),
        "win_rate": round(float((values > 0).mean() * 100), 4),
        "net_pct_points": round(float(values.sum()), 6),
        "mean_net_pct": round(float(values.mean()), 6),
        "profit_factor": round(gains / losses, 4) if losses else (999.0 if gains else 0.0),
        "max_drawdown_pct_points": round(float(drawdown.max()), 6),
        "median_exit_minutes": round(float(frame.exit_minute.median()), 2),
        "outcomes": frame.outcome.value_counts().to_dict(),
    }


def benchmark(args: argparse.Namespace) -> dict[str, Any]:
    candidates = _prepare_candidates(args.candidates)
    labelled: dict[str, list[pd.DataFrame]] = {name: [] for name in PROFILES}
    for symbol, scoped in candidates.groupby("symbol", sort=True):
        path = args.minute_dir / f"{symbol}.parquet"
        if not path.exists():
            continue
        minute = pd.read_parquet(
            path,
            columns=["open_time", "open", "high", "low", "close"],
        ).sort_values("open_time")
        for name, profile in PROFILES.items():
            labels = relabel_profile(minute, scoped, **profile)
            built = scoped.copy()
            built[["net_pct", "exit_minute", "outcome", "valid"]] = labels[
                ["net_pct", "exit_minute", "outcome", "valid"]
            ]
            labelled[name].append(built[built.valid].copy())

    all_times = pd.concat(labelled["current_fast"], ignore_index=True).time
    validation_start = all_times.quantile(0.60)
    test_start = all_times.quantile(0.80)
    report: dict[str, Any] = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "source_candidates": int(len(candidates)),
        "round_trip_cost_pct": ROUND_TRIP_COST_PCT,
        "validation_start": validation_start.isoformat(),
        "test_start": test_start.isoformat(),
        "profiles": {},
    }
    gates = {
        "v52_proxy": lambda frame: frame.baseline_passed.fillna(False),
        "v53_proxy": lambda frame: frame.v53_proxy,
        "rank95_proxy": lambda frame: frame.rank95,
    }
    for profile_name, parts in labelled.items():
        frame = pd.concat(parts, ignore_index=True).sort_values("time")
        profile_result: dict[str, Any] = {}
        for gate_name, gate in gates.items():
            admitted = frame[gate(frame)].copy()
            profile_result[gate_name] = {
                "development": _metrics(_schedule(admitted[admitted.time < validation_start])),
                "validation": _metrics(
                    _schedule(
                        admitted[
                            (admitted.time >= validation_start)
                            & (admitted.time < test_start)
                        ]
                    )
                ),
                "test": _metrics(_schedule(admitted[admitted.time >= test_start])),
            }
        report["profiles"][profile_name] = {
            "parameters": PROFILES[profile_name],
            "gates": profile_result,
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    report = benchmark(parse_args())
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
