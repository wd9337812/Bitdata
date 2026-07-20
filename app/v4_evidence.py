from __future__ import annotations

import json
from typing import Any


LEGACY_LIVE_ADMISSION_LANES = frozenset(
    {
        "core_canary",
        "core_provisional",
        "validated",
        "limited_exploration",
    }
)
V44_LIVE_ADMISSION_LANES = frozenset({"full_bet"})
RESEARCH_ONLY_ADMISSION_LANES = frozenset({"shadow_only", "exploration", "research"})


def admission_lane_from_row(row: dict[str, Any]) -> str:
    lane = str(row.get("admission_lane") or "").strip().lower()
    if lane:
        return lane
    try:
        payload = json.loads(row.get("payload") or "{}")
    except (json.JSONDecodeError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("admission_lane") or "").strip().lower()


def live_evidence_lanes(strategy_version: str) -> frozenset[str]:
    version = str(strategy_version or "").strip().lower()
    if version.startswith(("v4.4", "v4.5", "v4.6")):
        return V44_LIVE_ADMISSION_LANES
    return LEGACY_LIVE_ADMISSION_LANES


def is_live_eligible_v4_shadow(
    row: dict[str, Any],
    *,
    strategy_version: str,
    allow_unclassified_legacy: bool = False,
) -> bool:
    if str(row.get("evidence_type") or "decision").strip().lower() != "decision":
        return False
    lane = admission_lane_from_row(row)
    if lane in RESEARCH_ONLY_ADMISSION_LANES:
        return False
    if lane in live_evidence_lanes(strategy_version):
        return True
    return bool(allow_unclassified_legacy and not lane and not str(strategy_version).lower().startswith("v4.3"))


def filter_live_eligible_v4_shadows(
    rows: list[dict[str, Any]],
    *,
    strategy_version: str,
    allow_unclassified_legacy: bool = False,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        item = dict(row)
        if not is_live_eligible_v4_shadow(
            item,
            strategy_version=strategy_version,
            allow_unclassified_legacy=allow_unclassified_legacy,
        ):
            continue
        opportunity_id = str(item.get("opportunity_id") or "").strip()
        dedupe_key = opportunity_id or f"row:{item.get('id')}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        item["admission_lane"] = admission_lane_from_row(item) or "legacy_decision"
        result.append(item)
    return result


def executable_single_position_shadows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select the deterministic shadow path one real S0 account could execute.

    Rows are considered in opening order. While one selected shadow is open,
    overlapping candidates remain research evidence and cannot influence a
    release permit or recovery statistic.
    """
    ordered = sorted(
        (dict(row) for row in rows),
        key=lambda row: (str(row.get("opened_at") or row.get("closed_at") or ""), int(row.get("id") or 0)),
    )
    selected: list[dict[str, Any]] = []
    occupied_until = ""
    for row in ordered:
        opened_at = str(row.get("opened_at") or "")
        closed_at = str(row.get("closed_at") or opened_at)
        if not opened_at or (occupied_until and opened_at < occupied_until):
            continue
        selected.append(row)
        occupied_until = max(occupied_until, closed_at)
    return sorted(selected, key=lambda row: int(row.get("id") or 0), reverse=True)
