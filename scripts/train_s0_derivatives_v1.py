from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import train_s0_conditional_direction_v1 as base  # noqa: E402

MODEL_VERSION = "s0_derivatives_v1"
DEFAULT_SOURCE = ROOT / "data" / "research" / "s0_public_1m" / "s0_candidates_1m.parquet"
DEFAULT_MINUTE_DIR = ROOT / "data" / "research" / "s0_public_1m" / "parquet"
DEFAULT_DERIVATIVES_DIR = ROOT / "data" / "research" / "binance_um_derivatives"
DEFAULT_OUTPUT = ROOT / "data" / "research" / MODEL_VERSION
HORIZONS = {
    "10m": ("research_net_10m", 10),
    "30m": ("research_net_30m", 30),
    "60m": ("research_net_60m", 60),
}
BASE_NUMERIC = tuple(base.NUMERIC)
BASE_FEATURES = tuple(base.FEATURES)
BASE_PREPARE = base.prepare
DERIVATIVE_NUMERIC = (
    "deriv_metric_age_minutes",
    "deriv_oi_change_1",
    "deriv_oi_change_3",
    "deriv_oi_change_6",
    "deriv_oi_change_12",
    "deriv_oi_value_change_3",
    "deriv_oi_value_change_12",
    "deriv_top_account_bias",
    "deriv_top_account_change_3",
    "deriv_top_position_bias",
    "deriv_top_position_change_3",
    "deriv_global_account_bias",
    "deriv_global_account_change_3",
    "deriv_taker_bias",
    "deriv_taker_mean_3",
    "deriv_taker_mean_6",
    "deriv_taker_change_3",
    "deriv_taker_persistence_6",
    "deriv_funding_rate_pct",
    "deriv_funding_abs_pct",
    "deriv_funding_age_hours",
    "deriv_oi_rank",
    "deriv_oi_value_rank",
    "deriv_taker_rank",
    "deriv_top_position_rank",
    "deriv_oi_change_dir",
    "deriv_oi_price_alignment",
    "deriv_top_account_bias_dir",
    "deriv_top_position_bias_dir",
    "deriv_global_account_bias_dir",
    "deriv_taker_bias_dir",
    "deriv_funding_crowding_dir",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Train an isolated S0 candidate with Binance USD-M derivatives history."
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--minute-dir", type=Path, default=DEFAULT_MINUTE_DIR)
    parser.add_argument("--derivatives-dir", type=Path, default=DEFAULT_DERIVATIVES_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-train-rows", type=int, default=650_000)
    parser.add_argument(
        "--feature-set",
        choices=("combined", "derivatives"),
        default="combined",
    )
    return parser.parse_args()


def _safe_log_ratio(series: pd.Series) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce").clip(lower=0.05, upper=20.0)
    return np.log(values)


def build_metric_features(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.sort_values("timestamp_ms").copy()
    result["available_ms"] = result["timestamp_ms"].astype("int64") + 300_000
    oi = pd.to_numeric(result["sum_open_interest"], errors="coerce").clip(lower=1e-9)
    oi_value = pd.to_numeric(result["sum_open_interest_value"], errors="coerce").clip(lower=1e-9)
    result["deriv_oi_change_1"] = oi.pct_change(1, fill_method=None)
    result["deriv_oi_change_3"] = oi.pct_change(3, fill_method=None)
    result["deriv_oi_change_6"] = oi.pct_change(6, fill_method=None)
    result["deriv_oi_change_12"] = oi.pct_change(12, fill_method=None)
    result["deriv_oi_value_change_3"] = oi_value.pct_change(3, fill_method=None)
    result["deriv_oi_value_change_12"] = oi_value.pct_change(12, fill_method=None)
    ratios = {
        "deriv_top_account_bias": "count_toptrader_long_short_ratio",
        "deriv_top_position_bias": "sum_toptrader_long_short_ratio",
        "deriv_global_account_bias": "count_long_short_ratio",
        "deriv_taker_bias": "sum_taker_long_short_vol_ratio",
    }
    for target, source in ratios.items():
        result[target] = _safe_log_ratio(result[source])
    for target in (
        "deriv_top_account_bias",
        "deriv_top_position_bias",
        "deriv_global_account_bias",
    ):
        result[target.replace("_bias", "_change_3")] = result[target].diff(3)
    result["deriv_taker_mean_3"] = result.deriv_taker_bias.rolling(3, min_periods=2).mean()
    result["deriv_taker_mean_6"] = result.deriv_taker_bias.rolling(6, min_periods=3).mean()
    result["deriv_taker_change_3"] = result.deriv_taker_bias.diff(3)
    result["deriv_taker_persistence_6"] = (
        np.sign(result.deriv_taker_bias)
        .rolling(6, min_periods=3)
        .mean()
    )
    keep = ["available_ms", "timestamp_ms", *DERIVATIVE_NUMERIC[1:18]]
    return result[keep].replace([np.inf, -np.inf], np.nan)


def build_funding_features(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.sort_values("timestamp_ms").copy()
    result["funding_available_ms"] = result["timestamp_ms"].astype("int64") + 60_000
    result["deriv_funding_rate_pct"] = (
        pd.to_numeric(result["last_funding_rate"], errors="coerce") * 100.0
    )
    result["deriv_funding_abs_pct"] = result.deriv_funding_rate_pct.abs()
    return result[
        [
            "funding_available_ms",
            "timestamp_ms",
            "deriv_funding_rate_pct",
            "deriv_funding_abs_pct",
        ]
    ].rename(columns={"timestamp_ms": "funding_timestamp_ms"})


def merge_symbol_derivatives(
    candidates: pd.DataFrame,
    metrics: pd.DataFrame,
    funding: pd.DataFrame,
) -> pd.DataFrame:
    left = candidates.sort_values("open_time").copy()
    metric_features = build_metric_features(metrics)
    result = pd.merge_asof(
        left,
        metric_features.sort_values("available_ms"),
        left_on="open_time",
        right_on="available_ms",
        direction="backward",
        allow_exact_matches=True,
    )
    if funding.empty:
        result["funding_available_ms"] = np.nan
        result["funding_timestamp_ms"] = np.nan
        result["deriv_funding_rate_pct"] = 0.0
        result["deriv_funding_abs_pct"] = 0.0
    else:
        result = pd.merge_asof(
            result.sort_values("open_time"),
            build_funding_features(funding).sort_values("funding_available_ms"),
            left_on="open_time",
            right_on="funding_available_ms",
            direction="backward",
            allow_exact_matches=True,
        )
    result["deriv_metric_age_minutes"] = (
        result.open_time - result.timestamp_ms
    ) / 60_000.0
    result["deriv_funding_age_hours"] = (
        result.open_time - result.funding_timestamp_ms
    ) / 3_600_000.0
    return result


def enrich_derivatives(
    frame: pd.DataFrame,
    derivatives_dir: Path,
) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for symbol, scoped in frame.groupby("symbol", sort=True):
        metric_path = derivatives_dir / f"{symbol}-metrics.parquet"
        funding_path = derivatives_dir / f"{symbol}-funding.parquet"
        if not metric_path.exists():
            continue
        metrics = pd.read_parquet(metric_path)
        if metrics.empty:
            continue
        funding = pd.read_parquet(funding_path) if funding_path.exists() else pd.DataFrame()
        parts.append(merge_symbol_derivatives(scoped, metrics, funding))
    if not parts:
        return frame.iloc[:0].copy()
    result = pd.concat(parts, ignore_index=True).sort_values("open_time")
    valid = (
        result.deriv_metric_age_minutes.ge(5.0)
        & result.deriv_metric_age_minutes.le(15.0)
    )
    result = result[valid].copy()
    for column in (
        "deriv_oi_change_1",
        "deriv_oi_change_3",
        "deriv_oi_change_6",
        "deriv_oi_change_12",
        "deriv_oi_value_change_3",
        "deriv_oi_value_change_12",
    ):
        result[column] = pd.to_numeric(result[column], errors="coerce").clip(-0.5, 0.5)
    rank_columns = {
        "deriv_oi_change_3": "deriv_oi_rank",
        "deriv_oi_value_change_3": "deriv_oi_value_rank",
        "deriv_taker_bias": "deriv_taker_rank",
        "deriv_top_position_bias": "deriv_top_position_rank",
    }
    for source, target in rank_columns.items():
        result[target] = result.groupby("open_time", sort=False)[source].rank(pct=True)
    sign = np.where(result.direction.astype(str).eq("LONG"), 1.0, -1.0)
    result["deriv_oi_change_dir"] = result.deriv_oi_change_3 * sign
    result["deriv_oi_price_alignment"] = (
        result.deriv_oi_change_3 * pd.to_numeric(result.ret_3_dir, errors="coerce")
    )
    for column in (
        "deriv_top_account_bias",
        "deriv_top_position_bias",
        "deriv_global_account_bias",
        "deriv_taker_bias",
    ):
        result[f"{column}_dir"] = result[column] * sign
    result["deriv_funding_crowding_dir"] = result.deriv_funding_rate_pct * sign
    for column in DERIVATIVE_NUMERIC:
        result[column] = (
            pd.to_numeric(result.get(column), errors="coerce")
            .replace([np.inf, -np.inf], np.nan)
            .fillna(0.0)
        )
    return result


def configure(feature_set: str, derivatives_dir: Path) -> None:
    base.MODEL_VERSION = f"{MODEL_VERSION}_{feature_set}"
    base.DEFAULT_OUTPUT = DEFAULT_OUTPUT / feature_set
    base.HORIZONS = HORIZONS
    if feature_set == "combined":
        numeric = BASE_NUMERIC + DERIVATIVE_NUMERIC
    else:
        numeric = DERIVATIVE_NUMERIC
    base.NUMERIC = numeric
    base.FEATURES = numeric + tuple(base.CATEGORICAL)

    def prepare_derivatives(frame: pd.DataFrame) -> pd.DataFrame:
        configured_numeric = base.NUMERIC
        configured_features = base.FEATURES
        try:
            base.NUMERIC = BASE_NUMERIC
            base.FEATURES = BASE_FEATURES
            prepared = BASE_PREPARE(frame)
        finally:
            base.NUMERIC = configured_numeric
            base.FEATURES = configured_features
        return enrich_derivatives(prepared, derivatives_dir)

    base.prepare = prepare_derivatives


def train(args: argparse.Namespace) -> dict[str, Any]:
    configure(args.feature_set, args.derivatives_dir)
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
    report["method"] = (
        "Binance USD-M 5m OI, trader ratios, taker ratio and funding features; "
        "all metrics delayed before backward as-of joining; " + report["method"]
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
