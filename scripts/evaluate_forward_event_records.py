from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECORDS = ROOT / "data" / "research" / "s0_forward_event_monitor" / "records.jsonl"
COSTS: dict[str, float] = {
    "funding_extreme": 0.24,
    "volume_breakout": 0.24,
    "btc_impulse": 0.24,
    "momentum_confirmed": 0.60,
    "momentum_unconfirmed": 0.60,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate closed forward-event-shadow records per event type."
    )
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def evaluate(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_type: dict[str, list[float]] = {}
    for record in records:
        event_type = str(record.get("type", "unknown"))
        raw = float(record.get("raw_return_pct", 0.0))
        by_type.setdefault(event_type, []).append(raw)
    rows: dict[str, Any] = {}
    for event_type, values in sorted(by_type.items()):
        cost = COSTS.get(event_type, 0.24)
        net = [value - cost for value in values]
        wins = sum(value for value in net if value > 0)
        losses = -sum(value for value in net if value < 0)
        rows[event_type] = {
            "closed": len(values),
            "win_rate_pct": round(float(sum(value > 0 for value in net) / len(net) * 100.0), 2),
            "profit_factor": round(float(wins / losses), 3) if losses > 0 else (999.0 if wins > 0 else 0.0),
            "net_sum_pct": round(float(sum(net)), 3),
            "mean_net_pct": round(float(sum(net) / len(net)), 4),
            "mfe_mean_pct": round(
                float(sum(float(r.get("mfe_pct", 0.0)) for r in records if r.get("type") == event_type) / len(values)),
                3,
            ),
        }
    return {"records": len(records), "by_type": rows}


def main() -> None:
    args = parse_args()
    records: list[dict[str, Any]] = []
    if args.records.exists():
        with args.records.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    report = evaluate(records)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
