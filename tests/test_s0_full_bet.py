from __future__ import annotations

import pytest

from app.s0_full_bet import build_s0_full_bet_sizing, is_s0_full_bet


def _config(**overrides):
    return {
        "opportunity_v4_strategy_version": "v4.4",
        "opportunity_v44_full_bet_enabled": True,
        "opportunity_v44_margin_pct": 90.0,
        "opportunity_v44_min_risk_pct": 8.0,
        "opportunity_v44_max_risk_pct": 15.0,
        "opportunity_v44_stressed_risk_cap_pct": 15.0,
        "opportunity_v44_min_leverage": 3,
        "opportunity_v44_max_leverage": 10,
        "opportunity_v44_cost_stress_multiplier": 1.5,
        "opportunity_v44_loss_reduced_after": 2,
        "opportunity_v44_loss_reduced_risk_pct": 8.0,
        "taker_fee_pct_round_trip": 0.08,
        "estimated_slippage_pct": 0.03,
        "_stage_route": {"stage": "S0"},
        **overrides,
    }


def _candidate():
    return {
        "strategy_version": "v4.4",
        "opportunity_v4": {
            "strategy_version": "v4.4",
            "full_bet_admitted": True,
            "estimated_cost_pct": 0.14,
        },
    }


def test_full_bet_profile_is_scoped_to_v44_s0():
    candidate = _candidate()

    assert is_s0_full_bet(candidate, _config()) is True
    assert is_s0_full_bet(candidate, _config(_stage_route={"stage": "S1"})) is False
    assert is_s0_full_bet(candidate, _config(opportunity_v4_strategy_version="v4.3.2")) is False


def test_full_bet_requires_explicit_v44_admission():
    candidate = _candidate()
    candidate["opportunity_v4"]["full_bet_admitted"] = False

    assert is_s0_full_bet(candidate, _config()) is False
    result = build_s0_full_bet_sizing(
        equity=20.0,
        available_balance=20.0,
        entry=100.0,
        stop=99.15,
        requested_risk_pct=15.0,
        candidate=candidate,
        config=_config(),
    )
    assert result["applied"] is False


def test_full_bet_uses_margin_and_selects_leverage_within_target_risk():
    result = build_s0_full_bet_sizing(
        equity=20.0,
        available_balance=20.0,
        entry=100.0,
        stop=99.15,
        requested_risk_pct=15.0,
        candidate=_candidate(),
        config=_config(),
    )

    assert result["applied"] is True
    assert result["margin_utilization_pct"] == pytest.approx(90.0)
    assert 3 <= result["leverage"] <= 10
    assert result["stressed_risk_pct"] <= 15.0
    assert result["hard_risk_cap_pct"] == 15.0


def test_full_bet_reduces_margin_when_minimum_leverage_would_break_risk_cap():
    result = build_s0_full_bet_sizing(
        equity=20.0,
        available_balance=20.0,
        entry=100.0,
        stop=95.0,
        requested_risk_pct=8.0,
        candidate=_candidate(),
        config=_config(),
        consecutive_losses=2,
    )

    assert result["applied"] is True
    assert result["leverage"] == 3
    assert result["margin_reduced_for_risk"] is True
    assert result["margin_utilization_pct"] < 90.0
    assert result["target_risk_pct"] == 8.0
    assert result["stressed_risk_pct"] == pytest.approx(8.0)


def test_full_bet_never_uses_more_than_available_balance():
    result = build_s0_full_bet_sizing(
        equity=20.0,
        available_balance=12.0,
        entry=100.0,
        stop=99.15,
        requested_risk_pct=15.0,
        candidate=_candidate(),
        config=_config(),
    )

    assert result["margin_budget"] == pytest.approx(10.8)
    assert result["margin_used"] <= 10.8
    assert result["stressed_risk_pct"] <= 15.0


def test_v50_s30_uses_isolated_risk_profile_and_thirty_percent_cap():
    candidate = {
        "strategy_version": "v5.0-s30",
        "opportunity_v4": {
            "strategy_version": "v5.0-s30",
            "full_bet_admitted": True,
            "estimated_cost_pct": 0.14,
        },
    }
    config = {
        **_config(),
        "opportunity_v4_strategy_version": "v5.0-s30",
        "opportunity_v50_margin_pct": 90.0,
        "opportunity_v50_min_risk_pct": 12.0,
        "opportunity_v50_max_risk_pct": 30.0,
        "opportunity_v50_stressed_risk_cap_pct": 30.0,
        "opportunity_v50_min_leverage": 3,
        "opportunity_v50_max_leverage": 10,
        "opportunity_v50_cost_stress_multiplier": 1.5,
    }

    result = build_s0_full_bet_sizing(
        equity=20.0,
        available_balance=20.0,
        entry=100.0,
        stop=99.0,
        requested_risk_pct=45.0,
        candidate=candidate,
        config=config,
    )

    assert result["profile"] == "s0_full_bet_v50_s30"
    assert result["margin_utilization_pct"] <= 90.0
    assert result["target_risk_pct"] == 30.0
    assert result["hard_risk_cap_pct"] == 30.0
    assert result["stressed_risk_pct"] <= 30.0
    assert 3 <= result["leverage"] <= 10


def test_v52_caps_actual_risk_to_equity_above_hard_stop():
    candidate = {
        "strategy_version": "v5.2",
        "opportunity_v4": {
            "strategy_version": "v5.2",
            "full_bet_admitted": True,
            "estimated_cost_pct": 0.14,
        },
    }
    config = {
        **_config(),
        "opportunity_v4_strategy_version": "v5.2",
        "opportunity_v50_margin_pct": 90.0,
        "opportunity_v50_min_risk_pct": 12.0,
        "opportunity_v50_max_risk_pct": 30.0,
        "opportunity_v50_stressed_risk_cap_pct": 30.0,
        "opportunity_v50_min_leverage": 3,
        "opportunity_v50_max_leverage": 10,
        "opportunity_v50_cost_stress_multiplier": 1.5,
        "hard_stop_equity": 5.0,
        "opportunity_v52_hard_stop_reserve_usdt": 0.15,
    }

    result = build_s0_full_bet_sizing(
        equity=6.99,
        available_balance=6.99,
        entry=100.0,
        stop=99.0,
        requested_risk_pct=30.0,
        candidate=candidate,
        config=config,
    )

    expected_cap = (6.99 - 5.0 - 0.15) / 6.99 * 100
    assert result["configured_maximum_risk_pct"] == 30.0
    assert result["target_risk_pct"] == pytest.approx(expected_cap)
    assert result["stressed_risk_pct"] <= expected_cap
    assert result["hard_stop_headroom_cap_pct"] == pytest.approx(expected_cap)


def test_v52_can_use_full_thirty_percent_when_equity_has_headroom():
    candidate = {
        "strategy_version": "v5.2",
        "opportunity_v4": {
            "strategy_version": "v5.2",
            "full_bet_admitted": True,
            "estimated_cost_pct": 0.14,
        },
    }
    config = {
        **_config(),
        "opportunity_v4_strategy_version": "v5.2",
        "opportunity_v50_margin_pct": 90.0,
        "opportunity_v50_min_risk_pct": 12.0,
        "opportunity_v50_max_risk_pct": 30.0,
        "opportunity_v50_stressed_risk_cap_pct": 30.0,
        "opportunity_v50_min_leverage": 3,
        "opportunity_v50_max_leverage": 10,
        "opportunity_v50_cost_stress_multiplier": 1.5,
        "hard_stop_equity": 5.0,
        "opportunity_v52_hard_stop_reserve_usdt": 0.15,
    }

    result = build_s0_full_bet_sizing(
        equity=20.0,
        available_balance=20.0,
        entry=100.0,
        stop=99.0,
        requested_risk_pct=30.0,
        candidate=candidate,
        config=config,
    )

    assert result["target_risk_pct"] == 30.0
    assert result["stressed_risk_pct"] <= 30.0
