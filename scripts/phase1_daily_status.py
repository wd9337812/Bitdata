from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.evaluate_forward_event_records import TARGETS, evaluate  # noqa: E402

MONITOR_DIR = ROOT / "data" / "research" / "s0_forward_event_monitor"


def read_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if path.exists():
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    return records


def monitor_health() -> dict[str, Any]:
    pid_path = MONITOR_DIR / "monitor.pid"
    state_path = MONITOR_DIR / "state.json"
    pid = None
    if pid_path.exists():
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    alive = False
    if pid:
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            alive = False
    state_age = None
    if state_path.exists():
        state_age = int(time.time()) - int(state_path.stat().st_mtime)
    return {
        "pid": pid,
        "alive": alive,
        "state_age_seconds": state_age,
        "healthy": bool(alive and state_age is not None and state_age < 900),
    }


def shadows() -> dict[str, Any]:
    db = ROOT / "data" / "bitdata.db"
    result: dict[str, Any] = {}
    if not db.exists():
        return result
    try:
        con = sqlite3.connect(db)
        for family, version in (
            ("adaptive_30d_momentum", "s0_xmom_30d_paper_v2"),
            ("cross_sectional_momentum", "s0_xmom_24h_v2"),
        ):
            row = con.execute(
                "select count(*), sum(case when status='CLOSED' then 1 else 0 end), "
                "count(distinct symbol) from shadow_trades "
                "where strategy_family=? and strategy_version=?",
                (family, version),
            ).fetchone()
            result[f"{family}:{version}"] = {
                "total": int(row[0]),
                "closed": int(row[1] or 0),
                "symbols": int(row[2] or 0),
            }
        con.close()
    except Exception as exc:
        result["error"] = str(exc)
    return result


def git_status() -> dict[str, Any]:
    try:
        head = subprocess.run(
            ["git", "-C", str(ROOT), "log", "--oneline", "-1"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "-C", str(ROOT), "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
        )
        return {"head": head, "dirty": dirty}
    except Exception as exc:
        return {"error": str(exc)}


def main() -> None:
    records = read_records(MONITOR_DIR / "records.jsonl")
    report = evaluate(records)
    status = {
        "monitor": monitor_health(),
        "records": report["records"],
        "milestones": report["milestones"],
        "shadows": shadows(),
        "git": git_status(),
    }
    ready = [
        key
        for key, value in report["milestones"].items()
        if value["ready_for_evaluation"]
    ]
    print(json.dumps(status, ensure_ascii=False, indent=2))
    print("READY_FOR_EVALUATION:", ready or "none", flush=True)


if __name__ == "__main__":
    main()
