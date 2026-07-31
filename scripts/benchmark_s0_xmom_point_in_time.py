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

from scripts.benchmark_s0_cross_sectional_momentum import (
    COST_PCT,
    PROFILES,
    Profile,
    simulate,
    summarize,
    windows,
)


DEFAULT_DATA = ROOT / "data" / "research" / "binance_um_point_in_time_1h"
DEFAULT_COMPARISON = ROOT / "data" / "research" / "s0_public_1m" / "parquet"
DEFAULT_OUTPUT = (
    ROOT / "data" / "research" / "s0_xmom_point_in_time_audit"
)
DAILY_EXTENSION_MANIFEST = "daily_extension_manifest.json"
PROFILE_NAME = "momentum_24h_5pct"
MINIMUM_24H_QUOTE_VOLUME = 5_000_000.0
MINIMUM_UNIVERSE = 60


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare cross-sectional momentum on a point-in-time Binance "
            "universe against the previous surviving-symbol universe."
        )
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument(
        "--comparison-symbols-dir",
        type=Path,
        default=DEFAULT_COMPARISON,
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--minimum-age-days", type=int, default=30)
    return parser.parse_args()


def profile() -> Profile:
    return next(item for item in PROFILES if item.name == PROFILE_NAME)


def symbol_start_ms(record: dict[str, Any]) -> int:
    first_month = pd.Period(str(record["first_archive_month"]), freq="M")
    first_downloaded = pd.Period(
        str(record["archive_months"][0]),
        freq="M",
    )
    if first_month < first_downloaded:
        # Month start is conservative enough for the 30-day gate once an
        # instrument predates the downloaded window.
        return int(first_month.start_time.timestamp() * 1000)
    return int(record["first_open_time"])


def load_manifest(path: Path) -> tuple[dict[str, int], dict[str, Any]]:
    payload = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    if payload.get("failures"):
        raise ValueError(
            f"Point-in-time manifest contains {len(payload['failures'])} failures"
        )
    records = {
        str(item["symbol"]): item
        for item in payload.get("symbols", [])
    }
    extension_path = path / DAILY_EXTENSION_MANIFEST
    if extension_path.exists():
        extension = json.loads(extension_path.read_text(encoding="utf-8"))
        if extension.get("failures"):
            raise ValueError(
                "Point-in-time daily extension manifest contains "
                f"{len(extension['failures'])} failures"
            )
        for item in extension.get("symbols", []):
            records[str(item["symbol"])] = item
        payload["daily_extension"] = {
            "source": extension.get("source"),
            "start_date": extension.get("start_date"),
            "end_date": extension.get("end_date"),
            "checksum_verified": extension.get("checksum_verified"),
            "symbols": len(extension.get("symbols", [])),
        }
    starts = {
        str(item["symbol"]): symbol_start_ms(item)
        for item in records.values()
    }
    return starts, payload


def hourly_features(path: Path, start_ms: int) -> pd.DataFrame:
    frame = pd.read_parquet(
        path,
        columns=[
            "symbol",
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "quote_volume",
        ],
    ).sort_values("open_time")
    previous_close = frame.close.shift(1)
    true_range = pd.concat(
        [
            frame.high - frame.low,
            (frame.high - previous_close).abs(),
            (frame.low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    frame["atr_24h"] = true_range.rolling(24, min_periods=20).mean()
    for hours in (6, 24, 72):
        frame[f"ret_{hours}h"] = frame.close.pct_change(hours)
    frame["liquidity_24h"] = frame.quote_volume.rolling(
        24,
        min_periods=20,
    ).sum()
    frame["available_ms"] = frame.open_time.astype("int64") + 3_600_000
    frame["symbol_age_days"] = (
        frame.available_ms.astype("int64") - int(start_ms)
    ) / 86_400_000
    return frame.dropna(
        subset=[
            "ret_6h",
            "ret_24h",
            "ret_72h",
            "atr_24h",
            "liquidity_24h",
        ]
    )


def build_panel(data: Path, starts: dict[str, int]) -> pd.DataFrame:
    parts = [
        hourly_features(path, starts[path.stem])
        for path in sorted((data / "parquet").glob("*.parquet"))
        if path.stem in starts
    ]
    if not parts:
        raise ValueError(f"No point-in-time parquet files found in {data}")
    return (
        pd.concat(parts, ignore_index=True)
        .sort_values(["available_ms", "symbol"])
        .reset_index(drop=True)
    )


def selected_signals(
    panel: pd.DataFrame,
    minimum_age_days: int,
) -> tuple[pd.DataFrame, dict[str, int | float]]:
    btc = (
        panel.loc[panel.symbol.eq("BTCUSDT")]
        .set_index("available_ms")
        .ret_24h
    )
    eligible = panel.loc[
        ~panel.symbol.isin({"BTCUSDT", "ETHUSDT"})
        & panel.liquidity_24h.ge(MINIMUM_24H_QUOTE_VOLUME)
    ].copy()
    eligible["universe_size"] = eligible.groupby(
        "available_ms",
        sort=False,
    ).symbol.transform("size")
    breadth = eligible.groupby(
        "available_ms",
        sort=False,
    ).ret_24h.median()
    eligible["market_breadth_24h"] = eligible.available_ms.map(breadth)
    eligible["btc_ret_24h"] = eligible.available_ms.map(btc)
    eligible["market_direction"] = np.select(
        [
            eligible.market_breadth_24h.gt(0) & eligible.btc_ret_24h.gt(0),
            eligible.market_breadth_24h.lt(0) & eligible.btc_ret_24h.lt(0),
        ],
        ["LONG", "SHORT"],
        default="MIXED",
    )
    eligible = eligible.loc[
        eligible.universe_size.ge(MINIMUM_UNIVERSE)
        & eligible.market_direction.ne("MIXED")
    ].copy()
    eligible["momentum_rank"] = eligible.groupby(
        "available_ms",
        sort=False,
    ).ret_24h.rank(pct=True)
    eligible["directional_momentum"] = np.where(
        eligible.market_direction.eq("LONG"),
        eligible.ret_24h,
        -eligible.ret_24h,
    )
    selected = (
        eligible.sort_values(
            ["available_ms", "directional_momentum", "liquidity_24h"],
            ascending=[True, False, False],
        )
        .groupby("available_ms", sort=False)
        .head(1)
        .copy()
    )
    selected["direction"] = selected.market_direction
    selected["strength"] = np.where(
        selected.direction.eq("LONG"),
        selected.momentum_rank,
        1.0 - selected.momentum_rank,
    )
    age = panel.set_index(["available_ms", "symbol"]).symbol_age_days
    selected["symbol_age_days"] = [
        float(age.loc[(row.available_ms, row.symbol)])
        for row in selected.itertuples(index=False)
    ]
    young = selected.symbol_age_days.lt(minimum_age_days)
    return selected.loc[~young].copy(), {
        "minimum_24h_quote_volume": MINIMUM_24H_QUOTE_VOLUME,
        "minimum_universe": MINIMUM_UNIVERSE,
        "eligible_rows": int(len(eligible)),
        "selected_before_age_gate": int(len(selected)),
        "skipped_selected_too_young": int(young.sum()),
        "selected_after_age_gate": int((~young).sum()),
    }


def scope_report(
    panel: pd.DataFrame,
    strategy_profile: Profile,
    minimum_age_days: int,
) -> tuple[dict[str, Any], pd.DataFrame]:
    signals, gate = selected_signals(
        panel,
        minimum_age_days,
    )
    trades = simulate(
        signals,
        panel,
        strategy_profile,
        cost_pct=COST_PCT,
    )
    stressed = trades.copy()
    stressed["net_pct"] = stressed.gross_pct - 0.24
    coverage = panel.groupby("available_ms", sort=False).symbol.nunique()
    return {
        "panel_rows": int(len(panel)),
        "symbols": int(panel.symbol.nunique()),
        "hourly_universe": {
            "minimum": int(coverage.min()),
            "median": round(float(coverage.median()), 2),
            "maximum": int(coverage.max()),
        },
        "age_gate": gate,
        "baseline_cost_0_12pct": {
            "overall": summarize(trades),
            "windows": windows(trades),
        },
        "double_cost_0_24pct": {
            "overall": summarize(stressed),
            "windows": windows(stressed),
        },
    }, trades


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    starts, manifest = load_manifest(args.data)
    panel_path = args.output / "point_in_time_panel.parquet"
    if panel_path.exists():
        panel = pd.read_parquet(panel_path)
    else:
        panel = build_panel(args.data, starts)
        panel.to_parquet(panel_path, index=False, compression="zstd")
    comparison_symbols = {
        path.stem for path in args.comparison_symbols_dir.glob("*.parquet")
    }
    old_panel = panel.loc[panel.symbol.isin(comparison_symbols)].copy()
    strategy_profile = profile()
    full_report, full_trades = scope_report(
        panel,
        strategy_profile,
        args.minimum_age_days,
    )
    old_report, old_trades = scope_report(
        old_panel,
        strategy_profile,
        args.minimum_age_days,
    )
    selected_symbols = set(full_trades.symbol)
    old_symbols = set(old_trades.symbol)
    report = {
        "method": (
            "Official Binance USD-M monthly 1h archives; every crypto perpetual "
            "with a bar at that historical hour is considered. The live V2 "
            "flow is reproduced exactly: exclude BTC/ETH from the altcoin "
            "cross-section, require 5M USDT rolling 24h quote volume and at "
            "least 60 eligible altcoins, align BTC and median breadth, select "
            "the absolute strongest end, "
            "post-selection 30-day age gate without substitution, next-hour "
            "open, conservative stop-first OHLC replay."
        ),
        "archive_source": manifest.get("source"),
        "archive_months": [
            manifest.get("start_month"),
            manifest.get("end_month"),
        ],
        "checksum_verified": manifest.get("checksum_verified"),
        "profile": strategy_profile.__dict__,
        "point_in_time_universe": full_report,
        "previous_119_symbol_universe_on_same_hourly_data": old_report,
        "selection_difference": {
            "full_selected_symbols": len(selected_symbols),
            "old_selected_symbols": len(old_symbols),
            "newly_represented_selected_symbols": sorted(
                selected_symbols - comparison_symbols
            ),
        },
        "interpretation": (
            "The point-in-time result is authoritative for survivorship-bias "
            "direction. Minute-level execution remains required before any "
            "live qualification."
        ),
    }
    full_trades.assign(scope="point_in_time").to_parquet(
        args.output / "point_in_time_trades.parquet",
        index=False,
    )
    old_trades.assign(scope="previous_119").to_parquet(
        args.output / "previous_universe_trades.parquet",
        index=False,
    )
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
