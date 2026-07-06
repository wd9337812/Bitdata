from __future__ import annotations

from typing import Any


STAGE_PROFILES = [
    {
        "stage": "S0",
        "label": "\u6781\u9650\u6eda\u4ed3",
        "min_equity": 0.0,
        "max_equity": 100.0,
        "recommended_mode": "extreme_sprint",
        "risk_posture": "\u6700\u9ad8\u98ce\u9669",
        "max_open_positions": 1,
        "base_risk_key": "extreme_sprint_risk_per_trade_pct",
    },
    {
        "stage": "S1",
        "label": "\u6781\u9650\u51b2\u523a\u589e\u5f3a",
        "min_equity": 100.0,
        "max_equity": 500.0,
        "recommended_mode": "extreme_sprint",
        "risk_posture": "\u5f88\u9ad8\u98ce\u9669",
        "max_open_positions": 2,
        "base_risk_key": "extreme_sprint_risk_per_trade_pct",
    },
    {
        "stage": "S2",
        "label": "\u8fdb\u653b\u8f6e\u52a8",
        "min_equity": 500.0,
        "max_equity": 2_000.0,
        "recommended_mode": "attack",
        "risk_posture": "\u9ad8\u98ce\u9669",
        "max_open_positions": 2,
        "base_risk_key": "attack_risk_per_trade_pct",
    },
    {
        "stage": "S3",
        "label": "\u8d8b\u52bf/\u4e8b\u4ef6\u6df7\u5408",
        "min_equity": 2_000.0,
        "max_equity": 10_000.0,
        "recommended_mode": "tournament_sprint",
        "risk_posture": "\u4e2d\u9ad8\u98ce\u9669",
        "max_open_positions": 3,
        "base_risk_key": "tournament_sprint_risk_per_trade_pct",
    },
    {
        "stage": "S4",
        "label": "\u53d7\u63a7\u52a8\u91cf",
        "min_equity": 10_000.0,
        "max_equity": 100_000.0,
        "recommended_mode": "attack",
        "risk_posture": "\u4e2d\u7b49\u98ce\u9669",
        "max_open_positions": 4,
        "base_risk_key": "attack_risk_per_trade_pct",
    },
    {
        "stage": "S5",
        "label": "\u591a\u7b56\u7565\u7ec4\u5408",
        "min_equity": 100_000.0,
        "max_equity": 1_000_000.0,
        "recommended_mode": "balanced",
        "risk_posture": "\u4e2d\u4f4e\u98ce\u9669",
        "max_open_positions": 6,
        "base_risk_key": "risk_per_trade_pct",
    },
    {
        "stage": "S6",
        "label": "\u7f51\u683c\u7a33\u5b9a",
        "min_equity": 1_000_000.0,
        "max_equity": float("inf"),
        "recommended_mode": "grid",
        "risk_posture": "\u4f4e\u5230\u4e2d\u98ce\u9669",
        "max_open_positions": 10,
        "base_risk_key": "risk_per_trade_pct",
    },
]


def stage_profile_for_equity(equity: float | None, config: dict[str, Any] | None = None) -> dict[str, Any]:
    value = float(equity or 0.0)
    config = config or {}
    for profile in STAGE_PROFILES:
        if float(profile["min_equity"]) <= value < float(profile["max_equity"]):
            result = dict(profile)
            result["equity"] = value
            result["base_risk_pct"] = float(config.get(result["base_risk_key"], 0.0) or 0.0)
            result["summary"] = (
                f"{result['stage']} {result['label']}\uff1a\u5efa\u8bae {result['recommended_mode']}\uff0c"
                f"\u98ce\u9669\u6863\u4f4d {result['risk_posture']}"
            )
            return result
    return dict(STAGE_PROFILES[-1])


def all_stage_profiles(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return [stage_profile_for_equity(item["min_equity"], config) for item in STAGE_PROFILES]
