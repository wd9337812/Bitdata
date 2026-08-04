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

from scripts.benchmark_s0_point_in_time_slow_momentum import (  # noqa: E402
    add_slow_returns,
)
from scripts.benchmark_s0_xmom_point_in_time import (  # noqa: E402
    build_panel,
    load_manifest,
)

DATA_DIRS = (
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2020_2023",
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2024_2025",
    ROOT / "data" / "research" / "binance_um_point_in_time_1h",
)
FUNDING_DIRS = (
    ROOT / "data" / "research" / "binance_um_point_in_time_funding_2020_2023",
    ROOT / "data" / "research" / "binance_um_point_in_time_funding_2024_2025",
    ROOT / "data" / "research" / "binance_um_point_in_time_funding",
)
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_tail_event_mfe"
FORWARD_HOURS = 120
STOP_PCT = 0.12
ROUND_TRIP_COST_PCT = 0.0024
MIN_AGE_DAYS = 45
MIN_LIQUIDITY_24H = 20_000_000.0
LABEL_THRESHOLD_PCT = 2.0 * (STOP_PCT + ROUND_TRIP_COST_PCT)


def load_funding(dirs: tuple[Path, ...]) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for directory in dirs:
        if not directory.exists():
            continue
        for path in directory.glob("*-funding.parquet"):
            symbol = path.name.split("-funding.parquet")[0].upper()
            frame = pd.read_parquet(path)
            if frame.empty:
                continue
            frame = frame.sort_values("timestamp_ms").copy()
            frame["funding_available_ms"] = frame.timestamp_ms.astype("int64") + 60_000
            cols = frame[["funding_available_ms", "last_funding_rate"]]
            if symbol in frames:
                frames[symbol] = pd.concat(
                    [frames[symbol], cols], ignore_index=True
                ).drop_duplicates("funding_available_ms", keep="last")
            else:
                frames[symbol] = cols
    return frames


def attach_funding(panel: pd.DataFrame, funding: dict[str, pd.DataFrame]) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for symbol, scoped in panel.groupby("symbol", sort=False):
        frame = funding.get(symbol.upper())
        if frame is None or frame.empty:
            parts.append(scoped.assign(funding_rate_pct=float("nan")))
            continue
        merged = pd.merge_asof(
            scoped.sort_values("available_ms"),
            frame.sort_values("funding_available_ms"),
            left_on="available_ms",
            right_on="funding_available_ms",
            direction="backward",
        )
        merged["funding_rate_pct"] = (
            pd.to_numeric(merged.last_funding_rate, errors="coerce") * 100
        )
        parts.append(merged.drop(columns=["funding_available_ms", "last_funding_rate"]))
    return pd.concat(parts, ignore_index=True).sort_values(
        ["available_ms", "symbol"]
    ).reset_index(drop=True)


def extract_candidates(panel: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    day_ms = 86_400_000
    for symbol, g in panel.groupby("symbol", sort=False):
        g = g.sort_values("available_ms").reset_index(drop=True)
        available = g.available_ms.to_numpy(dtype="int64")
        opens = g.open.to_numpy(dtype="float64")
        highs = g.high.to_numpy(dtype="float64")
        lows = g.low.to_numpy(dtype="float64")
        closes = g.close.to_numpy(dtype="float64")
        qv = g.quote_volume.to_numpy(dtype="float64")
        atr = g.atr_24h.to_numpy(dtype="float64")
        age = g.symbol_age_days.to_numpy(dtype="float64")
        liq = g.liquidity_24h.to_numpy(dtype="float64")
        ret24 = g.ret_24h.to_numpy(dtype="float64")
        ret168 = g.ret_168h.to_numpy(dtype="float64")
        ret720 = g.ret_720h.to_numpy(dtype="float64")
        funding = g.funding_rate_pct.to_numpy(dtype="float64")
        n = len(g)
        for i in range(n):
            if available[i] % day_ms != 0:
                continue
            if age[i] < MIN_AGE_DAYS or liq[i] < MIN_LIQUIDITY_24H:
                continue
            if not np.isfinite(ret720[i]):
                continue
            if i + 1 >= n or i + FORWARD_HOURS >= n:
                continue
            entry = opens[i + 1]
            if not np.isfinite(entry) or entry <= 0:
                continue
            window_high = highs[i + 1 : i + 1 + FORWARD_HOURS]
            window_low = lows[i + 1 : i + 1 + FORWARD_HOURS]
            if len(window_high) < FORWARD_HOURS:
                continue
            mfe_up = float(window_high.max() / entry - 1.0)
            mfe_down = float(1.0 - window_low.min() / entry)
            mfe_max = max(mfe_up, mfe_down)
            trailing_24h = float(qv[max(0, i - 23) : i + 1].sum())
            trailing_480h = float(qv[max(0, i - 479) : i + 1].sum())
            volume_shock = trailing_24h / max(trailing_480h / max(1, len(qv[max(0, i - 479) : i + 1])), 1e-9)
            rows.append(
                {
                    "symbol": symbol,
                    "available_ms": int(available[i]),
                    "ret_24h": float(ret24[i]),
                    "ret_168h": float(ret168[i]),
                    "ret_720h": float(ret720[i]),
                    "atr_pct": float(atr[i] / entry) if np.isfinite(atr[i]) and atr[i] > 0 else float("nan"),
                    "volume_shock": volume_shock,
                    "funding_rate_pct": float(funding[i]) if np.isfinite(funding[i]) else float("nan"),
                    "mfe_up_pct": mfe_up * 100,
                    "mfe_down_pct": mfe_down * 100,
                    "mfe_max_pct": mfe_max * 100,
                    "label": int(mfe_max * 100 >= LABEL_THRESHOLD_PCT * 100),
                }
            )
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    funding = load_funding(FUNDING_DIRS)
    print(f"funding symbols: {len(funding)}", flush=True)
    parts: list[pd.DataFrame] = []
    for index, data_dir in enumerate(DATA_DIRS, start=1):
        starts, _ = load_manifest(data_dir)
        panel = build_panel(data_dir, starts)
        panel = add_slow_returns(panel)
        panel = panel.loc[panel.symbol.isin(funding)]
        parts.append(panel)
        print(f"dir{index}: panel rows {len(panel)}", flush=True)
        del panel
        gc.collect()
    panel = pd.concat(parts, ignore_index=True).reset_index(drop=True)
    panel = attach_funding(panel, funding)
    print(f"merged panel rows: {len(panel)}", flush=True)
    candidates = extract_candidates(panel)
    args.output.mkdir(parents=True, exist_ok=True)
    candidates.to_parquet(args.output / "candidates.parquet", index=False)
    candidates["year"] = pd.to_datetime(
        candidates.available_ms, unit="ms", utc=True
    ).dt.year
    baseline = candidates.groupby("year").agg(
        n=("label", "size"),
        hit_rate=("label", "mean"),
        mean_mfe=("mfe_max_pct", "mean"),
        p95_mfe=("mfe_max_pct", lambda x: x.quantile(0.95)),
    ).round(4)
    for feature in ("volume_shock", "ret_720h"):
        top = (
            candidates.sort_values(feature, ascending=False)
            .groupby("year", sort=False)
            .head(candidates.groupby("year").size().max() // 10)
        )
        top_stats = top.groupby("year").agg(
            n=("label", "size"),
            hit_rate=("label", "mean"),
            mean_mfe=("mfe_max_pct", "mean"),
        ).round(4)
        print(f"\n=== top-decile by {feature} ===")
        print(top_stats.to_string())
    print("\n=== baseline by year ===")
    print(baseline.to_string())
    summary = {
        "label_threshold_pct": LABEL_THRESHOLD_PCT * 100,
        "forward_hours": FORWARD_HOURS,
        "candidates": int(len(candidates)),
        "baseline": baseline.reset_index().to_dict("records"),
        "top_by_volume_shock": (
            candidates.sort_values("volume_shock", ascending=False)
            .groupby("year")
            .head(len(candidates) // 10)
            .groupby("year")["label"]
            .mean()
            .round(4)
            .to_dict()
        ),
        "top_by_ret720": (
            candidates.sort_values("ret_720h", ascending=False)
            .groupby("year")
            .head(len(candidates) // 10)
            .groupby("year")["label"]
            .mean()
            .round(4)
            .to_dict()
        ),
        "warning": "Baseline exploration only; not a live strategy.",
    }
    (args.output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
