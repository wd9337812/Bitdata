from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class EventOverrideCandidate:
    """A fully validated event-channel candidate (registry entry)."""

    name: str
    symbol: str
    direction: int  # 1 = long, -1 = short
    expected_net_pct: float
    evidence_pf: float
    evidence_trades: int
    source: str = "forward_shadow"


@dataclass(frozen=True)
class EventOverrideConfig:
    enabled: bool = False
    min_evidence_pf: float = 1.2
    min_evidence_trades: int = 30
    min_expected_net_pct: float = 0.0
    max_risk_pct: float = 30.0
    max_leverage: float = 5.0
    takeover_cost_cap_r: float = 2.0
    floating_profit_floor_r: float = 1.5


@dataclass
class EventOverrideState:
    config: EventOverrideConfig = field(default_factory=EventOverrideConfig)
    candidates: list[EventOverrideCandidate] = field(default_factory=list)

    def eligible(self) -> list[EventOverrideCandidate]:
        return [
            candidate
            for candidate in self.candidates
            if candidate.evidence_pf >= self.config.min_evidence_pf
            and candidate.evidence_trades >= self.config.min_evidence_trades
            and candidate.expected_net_pct >= self.config.min_expected_net_pct
        ]

    def decide(self) -> dict[str, Any]:
        """Return an override decision. Never places orders."""
        if not self.config.enabled:
            return {
                "enabled": False,
                "override": None,
                "reason": "channel_disabled",
            }
        eligible = self.eligible()
        if not eligible:
            return {
                "enabled": True,
                "override": None,
                "reason": "no_qualified_candidate",
            }
        best = max(eligible, key=lambda item: item.expected_net_pct)
        return {
            "enabled": True,
            "override": {
                "name": best.name,
                "symbol": best.symbol,
                "direction": best.direction,
                "risk_pct": self.config.max_risk_pct,
                "leverage": self.config.max_leverage,
            },
            "reason": "candidate_qualified",
            "dry_run": True,
        }

    def status_payload(self) -> dict[str, Any]:
        eligible = self.eligible()
        return {
            "enabled": self.config.enabled,
            "base_layer": "30日山寨动量",
            "base_risk_pct": 20,
            "base_leverage": 2,
            "base_exit": "3.5R / 5天",
            "event_channel_status": (
                "有合格候选" if eligible else "无合格候选"
            ),
            "qualified_candidates": [
                {
                    "name": item.name,
                    "symbol": item.symbol,
                    "direction": "LONG" if item.direction == 1 else "SHORT",
                    "evidence_pf": item.evidence_pf,
                    "evidence_trades": item.evidence_trades,
                    "expected_net_pct": item.expected_net_pct,
                }
                for item in eligible
            ],
            "takeover_rules": {
                "priority": "事件 > 底仓",
                "floating_profit_floor_r": self.config.floating_profit_floor_r,
                "takeover_cost_cap_r": self.config.takeover_cost_cap_r,
            },
            "data_source": "Binance 免费数据（归档/资金费/OI/taker/盘口）",
        }
