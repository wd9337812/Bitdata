from __future__ import annotations

from typing import Any


MARKET_STRUCTURE_SCHEMA = "market-structure-v1"

_SETUP_ALIASES = {
    "v3_breakout": "breakout",
    "v3_pullback": "pullback",
    "v3_prebreakout": "prebreakout",
    "v3_momentum": "momentum",
}


def normalize_setup_type(value: Any) -> str:
    raw = str(value or "unknown").lower()
    return _SETUP_ALIASES.get(raw, raw)


def market_structure(candidate: dict[str, Any]) -> dict[str, Any]:
    """Expose neutral market features while legacy V3 fields remain readable for archives."""
    existing = candidate.get("market_structure")
    if isinstance(existing, dict) and existing:
        return existing

    legacy = candidate.get("opportunity_v3") or {}
    state = candidate.get("market_state") or {}
    structure = {
        "schema_version": MARKET_STRUCTURE_SCHEMA,
        "market_regime": str(legacy.get("market_regime") or state.get("state") or "unknown").lower(),
        "market_regime_label": legacy.get("market_regime_label") or state.get("label"),
        "strength_percentile": float(legacy.get("strength_percentile") or 0.5),
        "direction_multiplier": float(legacy.get("direction_multiplier") or 0.8),
        "medium_ready": bool(legacy.get("medium_ready")),
        "medium_trend_aligned": bool(legacy.get("medium_trend_aligned")),
        "medium_path_efficiency": float(legacy.get("medium_path_efficiency") or 0.0),
        "setup_type": normalize_setup_type(candidate.get("entry_type")),
    }
    candidate["market_structure"] = structure
    return structure


def attach_market_structure(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for candidate in candidates:
        market_structure(candidate)
    return candidates


def build_market_structure_features(
    *,
    symbol: str,
    direction: str,
    signal: dict[str, Any],
    market_context: dict[str, Any],
    medium_context: dict[str, dict[str, float | bool]] | None = None,
) -> dict[str, Any]:
    """Build the neutral structure consumed by V4 without running a legacy tier model."""
    direction = direction.upper()
    symbol_context = (market_context.get("symbols") or {}).get(symbol.upper(), {})
    medium = (medium_context or {}).get(symbol.upper()) or {}
    strength_key = "short_strength_percentile" if direction == "SHORT" else "long_strength_percentile"
    return {
        "schema_version": MARKET_STRUCTURE_SCHEMA,
        "market_regime": str(market_context.get("regime") or "unknown").lower(),
        "market_regime_label": market_context.get("label") or "未知行情",
        "strength_percentile": float(symbol_context.get(strength_key) or 0.5),
        "direction_multiplier": float((market_context.get("direction_multipliers") or {}).get(direction, 0.85)),
        "medium_ready": bool(medium),
        "medium_trend_aligned": bool(medium.get("trend_short" if direction == "SHORT" else "trend_long")),
        "medium_path_efficiency": float(medium.get("path_efficiency") or 0.0),
        "setup_type": normalize_setup_type(signal.get("entry_type")),
        "entry_phase": str(signal.get("entry_phase") or "UNKNOWN").upper(),
    }
