from __future__ import annotations

from typing import Any


STRATEGY_GENERATION_ALIASES = {
    # V4.7.2 is a forward release built on the V4.11 execution and safety stack.
    "v4.7.2": "v4.11",
    # V4.7.3 keeps that safety stack and adds stricter expectancy plus fast exits.
    "v4.7.3": "v4.11",
    # V4.7.4 makes the candidate protection profile authoritative at runtime.
    "v4.7.4": "v4.11",
    # V5.0-S30 is a direct-live S0 release. It keeps the execution primitives
    # but owns a separate evidence and permit namespace.
    "v5.0-s30": "v5.0-s30",
    # V5.1 keeps the S0 execution boundary and replaces the all-or-nothing
    # panic veto with setup-aware, event-deduplicated admission.
    "v5.1": "v5.1",
    "v5.1.1": "v5.1.1",
    "v5.2": "v5.2",
    "v5.3": "v5.3",
}


V5_STRATEGY_FAMILY = "extreme_v5_roll"
V4_STRATEGY_FAMILY = "extreme_v4_roll"


def strategy_family_for_version(value: Any) -> str:
    version = str(value or "").strip().lower()
    return V5_STRATEGY_FAMILY if version.startswith("v5.") else V4_STRATEGY_FAMILY


def effective_strategy_version(value: Any) -> str:
    version = str(value or "").strip().lower()
    for prefix, generation in STRATEGY_GENERATION_ALIASES.items():
        if version.startswith(prefix):
            return generation
    return version


def strategy_supports(value: Any, capability: str) -> bool:
    raw_version = str(value or "").strip().lower()
    if raw_version.startswith("v5."):
        supported = {
            "full_bet",
            "continuous_permit",
            "direct_live",
            "v50_s30",
        }
        if raw_version.startswith(("v5.1", "v5.2", "v5.3")):
            supported.update(
                {
                    "v51_candidate_regime",
                    "v51_setup_router",
                    "event_group_dedupe",
                }
            )
        if raw_version.startswith(("v5.1.1", "v5.2", "v5.3")):
            supported.update({"exhaustion_reentry", "v511_incident_guard"})
        if raw_version.startswith(("v5.2", "v5.3")):
            supported.update({"v52_evidence_edge", "episode_evidence", "hard_stop_headroom"})
        if raw_version.startswith("v5.3"):
            supported.update({"v53_fusion", "adaptive_calibration", "global_adaptive", "global_market"})
        return capability in supported
    if capability == "v472_router":
        return raw_version.startswith(("v4.7.2", "v4.7.3", "v4.7.4"))
    version = effective_strategy_version(value)
    generations = {
        "full_bet": ("v4.4", "v4.5", "v4.6", "v4.7", "v4.8", "v4.9", "v4.10", "v4.11"),
        "continuous_permit": ("v4.5", "v4.6", "v4.7", "v4.8", "v4.9", "v4.10", "v4.11"),
        "adaptive_calibration": ("v4.7", "v4.8", "v4.9", "v4.10", "v4.11"),
        "exhaustion_reentry": ("v4.8", "v4.9", "v4.10", "v4.11"),
        "global_adaptive": ("v4.9", "v4.10", "v4.11"),
        "global_market": ("v4.10", "v4.11"),
        "healthy_continuation": ("v4.11",),
    }
    return version.startswith(generations.get(capability, ()))
