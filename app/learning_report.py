from __future__ import annotations

import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.telemetry import list_equity_snapshots, list_events, list_strategy_runs


def reports_dir() -> Path:
    config_path = Path(os.getenv("APP_CONFIG_PATH", "./data/config.json"))
    path = config_path.with_name("reports")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def build_daily_learning_report(now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=24)
    runs = [item for item in list_strategy_runs(1000) if (_parse_time(item.get("ts")) or now) >= since]
    snapshots = [item for item in list_equity_snapshots(1000) if (_parse_time(item.get("ts")) or now) >= since]
    events = [item for item in list_events(1000) if (_parse_time(item.get("ts")) or now) >= since]
    result_modes = Counter(str(item.get("result_mode") or "-") for item in runs)
    actions = Counter(str(item.get("action") or "-") for item in runs)
    blockers = Counter(str(item.get("reason") or "-") for item in runs if str(item.get("action") or "").upper() == "WAIT")
    by_symbol: dict[str, dict[str, Any]] = defaultdict(lambda: {"runs": 0, "live": 0, "wait": 0})
    for run in runs:
        symbol = str(run.get("symbol") or "-")
        by_symbol[symbol]["runs"] += 1
        if str(run.get("result_mode")) in {"live", "rotation_live"}:
            by_symbol[symbol]["live"] += 1
        if str(run.get("action") or "").upper() == "WAIT":
            by_symbol[symbol]["wait"] += 1
    equity_start = _float(snapshots[0].get("equity")) if snapshots else 0.0
    equity_end = _float(snapshots[-1].get("equity")) if snapshots else 0.0
    equity_change = equity_end - equity_start if snapshots else 0.0
    top_symbols = sorted(by_symbol.items(), key=lambda item: (item[1]["live"], item[1]["runs"]), reverse=True)[:8]
    reward = [symbol for symbol, data in top_symbols if data["live"] > 0][:5]
    penalize = [symbol for symbol, data in top_symbols if data["wait"] >= max(data["runs"] - 1, 1)][:5]
    lines = [
        f"# Bitdata \u6bcf\u65e5\u5b66\u4e60\u62a5\u544a - {now.date().isoformat()}",
        "",
        "## \u8d26\u6237\u6982\u51b5",
        f"- \u671f\u521d\u6743\u76ca: {equity_start:.4f} U",
        f"- \u671f\u672b\u6743\u76ca: {equity_end:.4f} U",
        f"- 24h \u53d8\u5316: {equity_change:.4f} U",
        "",
        "## \u7b56\u7565\u6267\u884c",
        f"- \u7b56\u7565\u8bb0\u5f55: {len(runs)}",
        f"- \u7ed3\u679c\u5206\u5e03: {dict(result_modes)}",
        f"- \u52a8\u4f5c\u5206\u5e03: {dict(actions)}",
        "",
        "## \u4e0d\u5f00\u4ed3\u4e3b\u8981\u539f\u56e0",
    ]
    lines.extend([f"- {reason}: {count}" for reason, count in blockers.most_common(8)] or ["- \u6682\u65e0"])
    lines.extend(["", "## \u5e01\u79cd\u5b66\u4e60"])
    for symbol, data in top_symbols:
        lines.append(f"- {symbol}: \u8bb0\u5f55 {data['runs']}, \u5b9e\u76d8 {data['live']}, \u7b49\u5f85 {data['wait']}")
    reward_text = ", ".join(reward) if reward else "\u6682\u65e0"
    penalize_text = ", ".join(penalize) if penalize else "\u6682\u65e0"
    lines.extend(
        [
            "",
            "## \u5efa\u8bae",
            f"- \u5956\u52b1\u5019\u9009: {reward_text}",
            f"- \u60e9\u7f5a/\u964d\u6743\u5019\u9009: {penalize_text}",
            "- \u5982\u679c\u5b9e\u76d8\u6b21\u6570\u8fc7\u5c11\uff0c\u5148\u68c0\u67e5\u963b\u585e\u539f\u56e0\u662f\u8d28\u91cf\u3001\u6210\u672c\u6bd4\u8fd8\u662f\u6ca1\u6709\u89e6\u53d1\u3002",
        ]
    )
    return {
        "generated_at": now.isoformat(),
        "runs": len(runs),
        "events": len(events),
        "equity_start": equity_start,
        "equity_end": equity_end,
        "equity_change": equity_change,
        "result_modes": dict(result_modes),
        "actions": dict(actions),
        "blockers": dict(blockers.most_common(12)),
        "reward_symbols": reward,
        "penalize_symbols": penalize,
        "markdown": "\n".join(lines) + "\n",
    }


def save_daily_learning_report(now: datetime | None = None) -> dict[str, Any]:
    report = build_daily_learning_report(now)
    parsed = _parse_time(report["generated_at"]) or datetime.now(timezone.utc)
    path = reports_dir() / f"daily-learning-{parsed.date().isoformat()}.md"
    path.write_text(report["markdown"], encoding="utf-8")
    report["path"] = str(path)
    return report


def latest_daily_learning_report() -> dict[str, Any]:
    files = sorted(reports_dir().glob("daily-learning-*.md"))
    if not files:
        return save_daily_learning_report()
    path = files[-1]
    return {"path": str(path), "markdown": path.read_text(encoding="utf-8")}
