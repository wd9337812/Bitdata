from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import train_s0_conditional_direction_v1 as base  # noqa: E402


MODEL_VERSION = "s0_conditional_direction_v2"
DEFAULT_OUTPUT = ROOT / "data" / "research" / MODEL_VERSION
HORIZONS = {
    "10m": ("research_net_10m", 10),
    "30m": ("research_net_30m", 30),
    "60m": ("research_net_60m", 60),
}


def configure() -> None:
    base.MODEL_VERSION = MODEL_VERSION
    base.DEFAULT_OUTPUT = DEFAULT_OUTPUT
    base.HORIZONS = HORIZONS


def main() -> None:
    configure()
    report = base.train(base.parse_args())
    print(base.json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

