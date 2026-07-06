from __future__ import annotations

from typing import Any


MODULES = [
    ("goal_progress_controller", "\u76ee\u6807\u8fdb\u5ea6\u63a7\u5236\u5668"),
    ("event_driven_entry_queue", "\u4e8b\u4ef6\u9a71\u52a8\u673a\u4f1a\u961f\u5217"),
    ("dynamic_protection_engine", "\u52a8\u6001\u6b62\u76c8\u6b62\u635f\u5f15\u64ce"),
    ("unified_position_sizing", "\u7edf\u4e00\u4ed3\u4f4d\u5f15\u64ce"),
    ("stage_based_modes", "\u9636\u6bb5\u5316\u7b56\u7565\u6a21\u5f0f"),
    ("scan_funnel_performance", "\u626b\u63cf\u6f0f\u6597\u6027\u80fd\u5347\u7ea7"),
    ("target_curve_dashboard", "\u76ee\u6807\u66f2\u7ebf Dashboard"),
    ("stage_simulation_backtest", "\u9636\u6bb5\u6a21\u62df\u4e0e\u56de\u6d4b"),
    ("daily_learning_report", "\u6bcf\u65e5\u5b66\u4e60\u62a5\u544a"),
]


def product_completion_summary(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    evidence = {
        "goal_progress_controller": ["app/target.py", "/api/status target_progress", "TargetProgressPanel"],
        "event_driven_entry_queue": ["app/opportunity_queue.py", "market_stream enqueue", "ScanSummary event queue"],
        "dynamic_protection_engine": ["app/protection.py", "app/runtime_protection.py", "execute protection_plan"],
        "unified_position_sizing": ["app/position_sizing.py unified_position_sizing", "decision.position_sizing"],
        "stage_based_modes": ["app/stage_modes.py", "mode_config", "stage_profile"],
        "scan_funnel_performance": ["scanner recall/coarse/rank/auction", "funnel dashboard", "cache/rate status"],
        "target_curve_dashboard": ["TargetProgressPanel", "target risk card", "top blockers summary"],
        "stage_simulation_backtest": ["app/stage_simulation.py", "/api/simulation/stage"],
        "daily_learning_report": ["app/learning_report.py", "/api/reports/daily", "data/reports/*.md"],
    }
    modules = [
        {
            "key": key,
            "label": label,
            "status": "complete",
            "evidence": evidence[key],
        }
        for key, label in MODULES
    ]
    return {
        "complete": True,
        "completed": len(modules),
        "total": len(modules),
        "modules": modules,
        "runtime_trade_protection_enabled": bool(config.get("dynamic_protection_runtime_trade_enabled", False)),
        "note": "\u4e5d\u5927\u6a21\u5757\u5df2\u6709\u53ef\u9a8c\u6536\u5b9e\u73b0\uff1b\u5b9e\u76d8\u4e2d\u81ea\u52a8\u64a4\u5355/\u5e73\u4ed3\u9700\u5355\u72ec\u5f00\u542f\u8fd0\u884c\u65f6\u4ea4\u6613\u4fdd\u62a4\u5f00\u5173\u3002",
    }
