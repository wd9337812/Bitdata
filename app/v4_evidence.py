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
    if version.startswith("v4.4"):
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
