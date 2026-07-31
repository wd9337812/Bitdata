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

MODEL_VERSION = "s0_aggflow_v1"
DEFAULT_SOURCE = derivatives.DEFAULT_SOURCE
DEFAULT_MINUTE_DIR = derivatives.DEFAULT_MINUTE_DIR
DEFAULT_DERIVATIVES_DIR = derivatives.DEFAULT_DERIVATIVES_DIR
DEFAULT_AGGFLOW_DIR = ROOT / "data" / "research" / "binance_um_aggtrades"
DEFAULT_OUTPUT = ROOT / "data" / "research" / MODEL_VERSION
HORIZONS = derivatives.HORIZONS
BASE_NUMERIC = derivatives.BASE_NUMERIC
BASE_FEATURES = derivatives.BASE_FEATURES
BASE_PREPARE = derivatives.BASE_PREPARE
AGGFLOW_RAW = (
    "agg_trade_count_1m",
    "agg_quote_log_1m",
    "agg_taker_imbalance_1m",
    "agg_signed_count_ratio_1m",
    "agg_mean_quote_1m",
    "agg_max_share_1m",
    "agg_size_hhi_1m",
    "agg_price_range_1m",
    "agg_price_return_1m",
    "agg_trade_count_5m",
    "agg_quote_log_5m",
    "agg_taker_imbalance_5m",
    "agg_mean_quote_5m",
    "agg_flow_persistence_5m",
    "agg_trade_count_15m",
    "agg_quote_log_15m",
    "agg_taker_imbalance_15m",
    "agg_mean_quote_15m",
    "agg_flow_persistence_15m",
)
AGGFLOW_NUMERIC = (
    "aggflow_age_minutes",
    *AGGFLOW_RAW,
    "agg_quote_rank_5m",
    "agg_mean_size_rank_5m",
    "agg_taker_imbalance_1m_dir",
    "agg_taker_imbalance_5m_dir",
    "agg_taker_imbalance_15m_dir",
    "agg_signed_count_ratio_1m_dir",
    "agg_flow_persistence_5m_dir",
    "agg_flow_persistence_15m_dir",
    "agg_price_return_1m_dir",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Train an isolated S0 candidate with Binance aggregate trade flow."
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--minute-dir", type=Path, default=DEFAULT_MINUTE_DIR)
    parser.add_argument("--derivatives-dir", type=Path, default=DEFAULT_DERIVATIVES_DIR)
    parser.add_argument("--aggflow-dir", type=Path, default=DEFAULT_AGGFLOW_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-train-rows", type=int, default=650_000)
    parser.add_argument(
        "--feature-set",
        choices=("aggflow", "base_aggflow", "derivatives_aggflow", "combined"),
        default="derivatives_aggflow",
    )
    return parser.parse_args()


def merge_symbol_aggflow(
    candidates: pd.DataFrame,
    aggflow: pd.DataFrame,
) -> pd.DataFrame:
    result = pd.merge_asof(
        candidates.sort_values("open_time").copy(),
        aggflow.sort_values("available_ms").copy(),
        left_on="open_time",
        right_on="available_ms",
        direction="backward",
        allow_exact_matches=True,
        suffixes=("", "_aggflow"),
    )
    result["aggflow_age_minutes"] = (
        result.open_time - result.available_ms
    ) / 60_000.0
    return result


def enrich_aggflow(frame: pd.DataFrame, aggflow_dir: Path) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for symbol, scoped in frame.groupby("symbol", sort=True):
        path = aggflow_dir / f"{symbol}-aggtrades.parquet"
        if not path.exists():
            continue
        flow = pd.read_parquet(path)
        if not flow.empty:
            parts.append(merge_symbol_aggflow(scoped, flow))
    if not parts:
        return frame.iloc[:0].copy()
    result = pd.concat(parts, ignore_index=True).sort_values("open_time")
    result = result[
        result.aggflow_age_minutes.ge(0.0)
        & result.aggflow_age_minutes.le(2.0)
        & pd.to_numeric(result.agg_trade_count_5m, errors="coerce").ge(5)
    ].copy()
    result["agg_quote_rank_5m"] = result.groupby("open_time", sort=False)[
        "agg_quote_log_5m"
    ].rank(pct=True)
    result["agg_mean_size_rank_5m"] = result.groupby("open_time", sort=False)[
        "agg_mean_quote_5m"
    ].rank(pct=True)
    sign = np.where(result.direction.astype(str).eq("LONG"), 1.0, -1.0)
    for column in (
        "agg_taker_imbalance_1m",
        "agg_taker_imbalance_5m",
        "agg_taker_imbalance_15m",
        "agg_signed_count_ratio_1m",
        "agg_flow_persistence_5m",
        "agg_flow_persistence_15m",
        "agg_price_return_1m",
    ):
        result[f"{column}_dir"] = pd.to_numeric(
            result[column], errors="coerce"
        ) * sign
    for column in AGGFLOW_NUMERIC:
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
    aggflow_dir: Path,
) -> None:
    base.MODEL_VERSION = f"{MODEL_VERSION}_{feature_set}"
    base.DEFAULT_OUTPUT = DEFAULT_OUTPUT / feature_set
    base.HORIZONS = HORIZONS
    if feature_set == "aggflow":
        numeric = AGGFLOW_NUMERIC
    elif feature_set == "base_aggflow":
        numeric = BASE_NUMERIC + AGGFLOW_NUMERIC
    elif feature_set == "derivatives_aggflow":
        numeric = derivatives.DERIVATIVE_NUMERIC + AGGFLOW_NUMERIC
    else:
        numeric = BASE_NUMERIC + derivatives.DERIVATIVE_NUMERIC + AGGFLOW_NUMERIC
    base.NUMERIC = numeric
    base.FEATURES = numeric + tuple(base.CATEGORICAL)

    def prepare_aggflow(frame: pd.DataFrame) -> pd.DataFrame:
        configured_numeric = base.NUMERIC
        configured_features = base.FEATURES
        try:
            base.NUMERIC = BASE_NUMERIC
            base.FEATURES = BASE_FEATURES
            prepared = BASE_PREPARE(frame)
        finally:
            base.NUMERIC = configured_numeric
            base.FEATURES = configured_features
        if feature_set in {"derivatives_aggflow", "combined"}:
            prepared = derivatives.enrich_derivatives(prepared, derivatives_dir)
        return enrich_aggflow(prepared, aggflow_dir)

    base.prepare = prepare_aggflow


def train(args: argparse.Namespace) -> dict[str, Any]:
    configure(args.feature_set, args.derivatives_dir, args.aggflow_dir)
    output = args.output / args.feature_set
    report = base.train(
        argparse.Namespace(
            source=args.source,
            minute_dir=args.minute_dir,
            output=output,
            max_train_rows=args.max_train_rows,
        )
    )
    report["feature_set"] = args.feature_set
    report["derivatives_source"] = str(args.derivatives_dir)
    report["aggflow_source"] = str(args.aggflow_dir)
    report["method"] = (
        "Official Binance USD-M aggregate trades, completed minute buckets, "
        "backward-only joins and taker-side flow; " + report["method"]
    )
    (output / f"{base.MODEL_VERSION}_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


def main() -> None:
    report = train(parse_args())
    print(
        json.dumps(
            {
                key: report.get(key)
                for key in (
                    "model_version",
                    "feature_set",
                    "selected_lane",
                    "validation_active_lanes",
                    "validation",
                    "untouched_test",
                    "cost_stress",
                    "release_checks",
                    "decision",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
