from __future__ import annotations

import json

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


def test_breakout_uses_dedicated_v12_expert(monkeypatch, tmp_path):
    model = tmp_path / "moe.joblib"
    model.write_bytes(b"placeholder")
    monkeypatch.setattr(
        s0_moe,
        "_load_bundle",
        lambda path: {
            "version": "s0_binance_moe_v1_2",
            "setup_aliases": {"breakout": "breakout"},
            "experts": {"breakout": {"classifier": object()}},
            "gate_floors": {},
        },
    )
    monkeypatch.setattr(
        s0_moe,
        "market_structure",
        lambda candidate: {"setup_type": "breakout", "market_regime": "broad_down"},
    )

    result = s0_moe.evaluate_candidate(
        {"direction": "LONG"},
        {"s0_moe_shadow_enabled": True, "s0_moe_model_path": str(model)},
    )

    assert result["setup_type"] == "breakout"
    assert result["expert"] == "breakout"
    assert result["active_gate"] is False


def test_online_metrics_only_treat_passed_rows_as_selected():
    rows = [
        {
            "status": "CLOSED",
            "opened_at": "2026-07-24T00:00:00+00:00",
            "symbol": "AUSDT",
            "market_regime": "quiet",
            "opportunity_id": "a",
            "net_pnl": 2.0,
            "estimated_cost": 0.2,
            "payload": json.dumps(
                {"moe": {"enabled": True, "active_gate": True, "passed": True, "expert": "momentum"}}
            ),
        },
        {
            "status": "CLOSED",
            "opened_at": "2026-07-24T00:01:00+00:00",
            "symbol": "BUSDT",
            "market_regime": "broad_down",
            "opportunity_id": "b",
            "net_pnl": -9.0,
            "estimated_cost": 0.3,
            "payload": json.dumps(
                {"moe": {"enabled": True, "active_gate": False, "passed": False, "expert": "momentum"}}
            ),
        },
    ]

    result = s0_moe._aggregate_online_evidence(rows)

    assert result["evaluated"]["closed"] == 2
    assert result["evaluated"]["net_pnl"] == -7.0
    assert result["selected"]["closed"] == 1
    assert result["selected"]["net_pnl"] == 2.0
    assert result["selected"]["profit_factor"] == 999.0
