from __future__ import annotations

import app.s0_moe as s0_moe


def test_moe_shadow_attachment_never_changes_live_admission(monkeypatch):
    candidate = {
        "symbol": "ALTUSDT",
        "direction": "LONG",
        "passed": True,
        "risk_pct": 12.0,
        "opportunity_v4": {"enabled": True, "rank_percentile": 0.99, "full_bet_admitted": True},
    }
    monkeypatch.setattr(
        s0_moe,
        "evaluate_candidate",
        lambda item, config: {
            "enabled": True,
            "mode": "shadow_only",
            "passed": False,
            "affects_live_admission": False,
        },
    )

    result = s0_moe.attach_moe_shadow([candidate], {"s0_moe_shadow_enabled": True})

    assert result[0]["passed"] is True
    assert result[0]["risk_pct"] == 12.0
    assert result[0]["opportunity_v4"]["full_bet_admitted"] is True
    assert result[0]["opportunity_v4"]["moe"]["passed"] is False
