from __future__ import annotations

import argparse
import gzip
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.training_lineage import training_data_quality, training_dataset


DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_moe_snapshots"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export a versioned S0 MoE online evidence snapshot without retraining live."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=50_000)
    parser.add_argument("--strategy-version", default="v4.11")
    parser.add_argument("--model-version", default="s0_binance_moe_v1_1")
    return parser.parse_args()


def _serializable(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "source_weight": 3.0 if row.get("live_trade_id") else 1.0,
        "source_kind": "live" if row.get("live_trade_id") else "shadow",
    }


def main() -> None:
    args = parse_args()
    generated_at = datetime.now(timezone.utc)
    stamp = generated_at.strftime("%Y%m%dT%H%M%SZ")
    args.output.mkdir(parents=True, exist_ok=True)
    rows = training_dataset(limit=max(1, min(args.limit, 50_000)))
    data_path = args.output / f"s0_moe_online_{stamp}.jsonl.gz"
    with gzip.open(data_path, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(_serializable(row), ensure_ascii=False, default=str))
            handle.write("\n")
    manifest = {
        "generated_at": generated_at.isoformat(),
        "strategy_version": args.strategy_version,
        "model_version": args.model_version,
        "rows": len(rows),
        "weighting": {
            "live": 3.0,
            "shadow": 1.0,
            "note": "Weights are training metadata only and never change live admission.",
        },
        "quality": training_data_quality(),
        "data_file": data_path.name,
        "automatic_live_replacement": False,
    }
    manifest_path = args.output / f"s0_moe_online_{stamp}.manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
