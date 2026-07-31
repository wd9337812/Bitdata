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

from scripts import train_s0_conditional_direction_v1 as base  # noqa: E402
from scripts import train_s0_derivatives_v1 as derivatives  # noqa: E402

MODEL_VERSION = "s0_orderbook_v1"
DEFAULT_SOURCE = derivatives.DEFAULT_SOURCE
DEFAULT_MINUTE_DIR = derivatives.DEFAULT_MINUTE_DIR
DEFAULT_DERIVATIVES_DIR = derivatives.DEFAULT_DERIVATIVES_DIR
DEFAULT_BOOK_DIR = ROOT / "data" / "research" / "binance_um_book_depth"
DEFAULT_OUTPUT = ROOT / "data" / "research" / MODEL_VERSION
HORIZONS = derivatives.HORIZONS
BASE_NUMERIC = derivatives.BASE_NUMERIC
BASE_FEATURES = derivatives.BASE_FEATURES
BASE_PREPARE = derivatives.BASE_PREPARE
BOOK_RAW = (
    "book_snapshot_count",
    "book_near_band_available",
    "book_imbalance_0.2_mean",
    "book_imbalance_0.2_last",
    "book_imbalance_0.2_std",
    "book_imbalance_0.2_persistence",
    "book_depth_log_0.2_mean",
    "book_imbalance_1_mean",
    "book_imbalance_1_last",
    "book_imbalance_1_std",
    "book_imbalance_1_persistence",
    "book_depth_log_1_mean",
    "book_imbalance_5_mean",
    "book_imbalance_5_last",
    "book_imbalance_5_std",
    "book_imbalance_5_persistence",
    "book_depth_log_5_mean",
    "book_near_share_mean",
    "book_imbalance_0.2_slope",
)
BOOK_NUMERIC = (
    "book_age_minutes",
    *BOOK_RAW,
    "book_near_depth_rank",
    "book_near_share_rank",
    "book_imbalance_0.2_mean_dir",
    "book_imbalance_0.2_last_dir",
    "book_imbalance_0.2_persistence_dir",
    "book_imbalance_1_mean_dir",
    "book_imbalance_1_persistence_dir",
    "book_imbalance_5_mean_dir",
    "book_imbalance_0.2_slope_dir",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Train an isolated S0 candidate with official Binance depth bands."
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--minute-dir", type=Path, default=DEFAULT_MINUTE_DIR)
    parser.add_argument("--derivatives-dir", type=Path, default=DEFAULT_DERIVATIVES_DIR)
    parser.add_argument("--book-dir", type=Path, default=DEFAULT_BOOK_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-train-rows", type=int, default=650_000)
    parser.add_argument(
        "--feature-set",
        choices=("orderbook", "base_orderbook", "derivatives_orderbook", "combined"),
        default="derivatives_orderbook",
    )
    return parser.parse_args()


def merge_symbol_book(
    candidates: pd.DataFrame,
    book: pd.DataFrame,
) -> pd.DataFrame:
    left = candidates.sort_values("open_time").copy()
    right = book.sort_values("available_ms").copy()
    result = pd.merge_asof(
        left,
        right,
        left_on="open_time",
        right_on="available_ms",
        direction="backward",
        allow_exact_matches=True,
        suffixes=("", "_book"),
    )
    result["book_age_minutes"] = (
        result.open_time - result.available_ms
    ) / 60_000.0
    return result


def enrich_orderbook(frame: pd.DataFrame, book_dir: Path) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for symbol, scoped in frame.groupby("symbol", sort=True):
        path = book_dir / f"{symbol}-book-depth.parquet"
        if not path.exists():
            continue
        book = pd.read_parquet(path)
        if book.empty:
            continue
        parts.append(merge_symbol_book(scoped, book))
    if not parts:
        return frame.iloc[:0].copy()
    result = pd.concat(parts, ignore_index=True).sort_values("open_time")
    valid = (
        result.book_age_minutes.ge(0.0)
        & result.book_age_minutes.le(10.0)
        & pd.to_numeric(result.book_snapshot_count, errors="coerce").ge(5)
    )
    result = result[valid].copy()
    result["book_near_depth_rank"] = result.groupby("open_time", sort=False)[
        "book_depth_log_0.2_mean"
    ].rank(pct=True)
    result["book_near_share_rank"] = result.groupby("open_time", sort=False)[
        "book_near_share_mean"
    ].rank(pct=True)
    sign = np.where(result.direction.astype(str).eq("LONG"), 1.0, -1.0)
    for column in (
        "book_imbalance_0.2_mean",
        "book_imbalance_0.2_last",
        "book_imbalance_0.2_persistence",
        "book_imbalance_1_mean",
        "book_imbalance_1_persistence",
        "book_imbalance_5_mean",
        "book_imbalance_0.2_slope",
    ):
        result[f"{column}_dir"] = pd.to_numeric(
            result[column], errors="coerce"
        ) * sign
    for column in BOOK_NUMERIC:
        if column not in result:
            result[column] = 0.0
        result[column] = (
            pd.to_numeric(result[column], errors="coerce")
            .replace([np.inf, -np.inf], np.nan)
            .fillna(0.0)
        )
    return result


def configure(
    feature_set: str,
    derivatives_dir: Path,
    book_dir: Path,
) -> None:
    base.MODEL_VERSION = f"{MODEL_VERSION}_{feature_set}"
    base.DEFAULT_OUTPUT = DEFAULT_OUTPUT / feature_set
    base.HORIZONS = HORIZONS
    if feature_set == "orderbook":
        numeric = BOOK_NUMERIC
    elif feature_set == "base_orderbook":
        numeric = BASE_NUMERIC + BOOK_NUMERIC
    elif feature_set == "derivatives_orderbook":
        numeric = derivatives.DERIVATIVE_NUMERIC + BOOK_NUMERIC
    else:
        numeric = BASE_NUMERIC + derivatives.DERIVATIVE_NUMERIC + BOOK_NUMERIC
    base.NUMERIC = numeric
    base.FEATURES = numeric + tuple(base.CATEGORICAL)

    def prepare_orderbook(frame: pd.DataFrame) -> pd.DataFrame:
        configured_numeric = base.NUMERIC
        configured_features = base.FEATURES
        try:
            base.NUMERIC = BASE_NUMERIC
            base.FEATURES = BASE_FEATURES
            prepared = BASE_PREPARE(frame)
        finally:
            base.NUMERIC = configured_numeric
            base.FEATURES = configured_features
        if feature_set in {"derivatives_orderbook", "combined"}:
            prepared = derivatives.enrich_derivatives(prepared, derivatives_dir)
        return enrich_orderbook(prepared, book_dir)

    base.prepare = prepare_orderbook


def train(args: argparse.Namespace) -> dict[str, Any]:
    configure(
        args.feature_set,
        args.derivatives_dir,
        args.book_dir,
    )
    output = args.output / args.feature_set
    base_args = argparse.Namespace(
        source=args.source,
        minute_dir=args.minute_dir,
        output=output,
        max_train_rows=args.max_train_rows,
    )
    report = base.train(base_args)
    report["feature_set"] = args.feature_set
    report["derivatives_source"] = str(args.derivatives_dir)
    report["book_source"] = str(args.book_dir)
    report["method"] = (
        "Official Binance USD-M 30-second depth bands aggregated to completed "
        "5m buckets and joined backward only; " + report["method"]
    )
    report_path = output / f"{base.MODEL_VERSION}_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


def main() -> None:
    report = train(parse_args())
    summary = {
        key: report.get(key)
        for key in (
            "model_version",
            "feature_set",
            "selected_lane",
            "validation",
            "untouched_test",
            "cost_stress",
            "release_checks",
            "decision",
        )
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
