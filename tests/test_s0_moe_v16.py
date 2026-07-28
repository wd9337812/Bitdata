from __future__ import annotations

import pandas as pd

from scripts.train_s0_moe_v1_6 import _deduplicate, metrics


def test_v16_deduplicates_shadow_and_live_with_live_precedence():
    frame = pd.DataFrame(
        [
            {
                "opportunity_id": "same",
                "source": "v5_shadow_decision",
                "time": pd.Timestamp("2026-07-28T00:00:00Z"),
                "net_pct": -1.0,
            },
            {
                "opportunity_id": "same",
                "source": "v5_live",
                "time": pd.Timestamp("2026-07-28T00:01:00Z"),
                "net_pct": 2.0,
            },
            {
                "opportunity_id": "other",
                "source": "v5_shadow_exploration",
                "time": pd.Timestamp("2026-07-28T00:02:00Z"),
                "net_pct": 0.5,
            },
        ]
    )

    result, removed = _deduplicate(frame)

    assert removed == 1
    assert len(result) == 2
    assert result.loc[result.opportunity_id.eq("same"), "source"].item() == "v5_live"


def test_v16_metrics_exposes_cost_aware_profit_factor_and_drawdown():
    frame = pd.DataFrame(
        {
            "net_stress_1_5": [1.0, -0.5, 0.25],
            "symbol": ["AUSDT", "BUSDT", "AUSDT"],
            "market_regime": ["mixed", "mixed", "panic"],
            "source": ["v5_live", "v5_shadow_decision", "v5_shadow_decision"],
        }
    )

    result = metrics(frame, "net_stress_1_5", "test")

    assert result["trades"] == 3
    assert result["net_pct_points"] == 0.75
    assert result["profit_factor"] == 2.5
    assert result["max_drawdown_pct_points"] == 0.5
