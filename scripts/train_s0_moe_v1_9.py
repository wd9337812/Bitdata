from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import train_s0_moe_v1_8 as v18  # noqa: E402


MODEL_VERSION = "s0_binance_moe_v1_9"
DEFAULT_VPS_HISTORY = ROOT / "data" / "research" / "vps_history_20260730_v5"
DEFAULT_OUTPUT = ROOT / "data" / "research" / MODEL_VERSION


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the million-scale MoE research retrain with the latest isolated VPS V5 history."
    )
    parser.add_argument(
        "--public-source",
        type=Path,
        default=v18.PUBLIC_SOURCE,
    )
    parser.add_argument(
        "--vps-history",
        type=Path,
        default=DEFAULT_VPS_HISTORY,
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
    )
    parser.add_argument("--max-public-per-expert", type=int, default=250_000)
    parser.add_argument("--folds", type=int, default=3)
    return parser.parse_args()


def main() -> None:
    # Reuse the audited v1.8 pipeline while isolating the artifact and release label.
    v18.MODEL_VERSION = MODEL_VERSION
    args = parse_args()
    report = v18.train(args)
    print(v18.json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
