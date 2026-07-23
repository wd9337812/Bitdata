from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.training_lineage import training_data_quality, training_dataset


def main() -> None:
    parser = argparse.ArgumentParser(description="导出 Bitdata 可追溯训练样本。")
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--output", default="data/training/s0_lineage.jsonl")
    args = parser.parse_args()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = training_dataset(args.limit)
    with output.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")

    quality = training_data_quality()
    print(
        json.dumps(
            {"output": str(output.resolve()), "rows": len(rows), "quality": quality},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
