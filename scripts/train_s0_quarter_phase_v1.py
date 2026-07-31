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

MODEL_VERSION = "s0_quarter_phase_v1"
DEFAULT_OUTPUT = ROOT / "data" / "research" / MODEL_VERSION
HORIZONS = {
    "240m": ("research_net_240m", 240),
    "480m": ("research_net_480m", 480),
    "720m": ("research_net_720m", 720),
}
PHASE_NUMERIC = (
    "phase_is_quarter_open",
    "phase_is_hour_open",
    "phase_is_funding_window",
    "phase_hour_sin",
    "phase_hour_cos",
    "phase_taker_dir",
    "phase_taker_change_dir",
    "phase_oi_change_dir",
    "phase_public_signal_dir",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Test quarter-hour Binance order-flow effects at 4-12h horizons."
    )
    parser.add_argument("--source", type=Path, default=derivatives.DEFAULT_SOURCE)
    parser.add_argument("--minute-dir", type=Path, default=derivatives.DEFAULT_MINUTE_DIR)
    parser.add_argument(
        "--derivatives-dir",
        type=Path,
        default=derivatives.DEFAULT_DERIVATIVES_DIR,
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-train-rows", type=int, default=650_000)
    return parser.parse_args()


def add_phase_features(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    timestamp = pd.to_datetime(result.timestamp_ms, unit="ms", utc=True)
    minute = timestamp.dt.minute
    hour = timestamp.dt.hour
    result["phase_is_quarter_open"] = minute.mod(15).eq(0).astype("int8")
    result["phase_is_hour_open"] = minute.eq(0).astype("int8")
    result["phase_is_funding_window"] = (
        minute.eq(0) & hour.isin([0, 8, 16])
    ).astype("int8")
    angle = 2.0 * np.pi * (hour + minute / 60.0) / 24.0
    result["phase_hour_sin"] = np.sin(angle)
    result["phase_hour_cos"] = np.cos(angle)
    sign = np.where(result.direction.astype(str).eq("LONG"), 1.0, -1.0)
    quarter = result.phase_is_quarter_open
    result["phase_taker_dir"] = result.deriv_taker_bias * sign * quarter
    result["phase_taker_change_dir"] = result.deriv_taker_change_3 * sign * quarter
    result["phase_oi_change_dir"] = result.deriv_oi_change_3 * sign * quarter
    result["phase_public_signal_dir"] = (
        pd.to_numeric(result.ret_24_dir, errors="coerce").fillna(0.0)
        + pd.to_numeric(result.volume_persistence, errors="coerce").fillna(0.0)
        + pd.to_numeric(result.directed_flow, errors="coerce").fillna(0.0)
    ) * quarter
    for column in PHASE_NUMERIC:
        result[column] = (
            pd.to_numeric(result[column], errors="coerce")
            .replace([np.inf, -np.inf], np.nan)
            .fillna(0.0)
        )
    return result


def configure(derivatives_dir: Path) -> None:
    base.MODEL_VERSION = MODEL_VERSION
    base.DEFAULT_OUTPUT = DEFAULT_OUTPUT
    base.HORIZONS = HORIZONS
    numeric = derivatives.DERIVATIVE_NUMERIC + PHASE_NUMERIC
    base.NUMERIC = numeric
    base.FEATURES = numeric + tuple(base.CATEGORICAL)

    def prepare_phase(frame: pd.DataFrame) -> pd.DataFrame:
        configured_numeric = base.NUMERIC
        configured_features = base.FEATURES
        try:
            base.NUMERIC = derivatives.BASE_NUMERIC
            base.FEATURES = derivatives.BASE_FEATURES
            prepared = derivatives.BASE_PREPARE(frame)
        finally:
            base.NUMERIC = configured_numeric
            base.FEATURES = configured_features
        enriched = derivatives.enrich_derivatives(prepared, derivatives_dir)
        return add_phase_features(enriched)

    base.prepare = prepare_phase


def train(args: argparse.Namespace) -> dict[str, Any]:
    configure(args.derivatives_dir)
    base_args = argparse.Namespace(
        source=args.source,
        minute_dir=args.minute_dir,
        output=args.output,
        max_train_rows=args.max_train_rows,
    )
    report = base.train(base_args)
    report["derivatives_source"] = str(args.derivatives_dir)
    report["method"] = (
        "Paper-inspired quarter-hour Binance futures order-flow interactions "
        "tested at 4h, 8h and 12h maximum holds; " + report["method"]
    )
    report_path = args.output / f"{MODEL_VERSION}_report.json"
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
