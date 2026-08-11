from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.opportunity_v4 import (
    V462_FEATURE_WEIGHTS,
    V411_FEATURE_WEIGHTS,
    V52_FEATURE_WEIGHTS,
    V53_FEATURE_WEIGHTS,
    _continuation_shape,
    _load_evidence,
    _model_expectancy,
    _model_features,
    _protection_profile,
    _regime_policy,
    _v48_reentry_policy,
    attach_v4_rankings,
    clear_v4_evidence_cache,
    v44_position_confidence,
)
from app.position_sizing import effective_position_risk
from app.scanner import _apply_v4_live_selection
from app.shadow_trading import ensure_shadow_tables
from app.telemetry import connect
from app.strategy_capabilities import strategy_supports


def _candidate(symbol: str, strength: float, volume: float) -> dict:
    return {
        "symbol": symbol,
        "direction": "LONG",
        "entry_type": "v3_breakout",
        "score": strength * 100,
        "signal": {
            "signal": "LONG",
            "last_price": 100,
            "stop": 99,
            "take_profit": 103,
            "expected_profit_pct": 3,
            "volume_acceleration": volume,
            "directed_trade_flow": 0.62,
            "breakout_extension_atr": 0.2,
            "impulse_atr": 0.5,
            "adverse_wick_ratio": 0.2,
            "entry_phase": "RETEST",
        },
        "expected_profit_pct": 3,
        "estimated_cost_pct": 0.12,
        "depth": {"spread_pct": 0.02, "depth_notional": 20_000},
        "market_state": {"state": "broad_up"},
        "opportunity_v3": {
            "strength_percentile": strength,
            "direction_multiplier": 1.1,
            "market_regime": "broad_up",
            "medium_ready": True,
            "medium_trend_aligned": True,
            "medium_path_efficiency": 0.3,
        },
        "execution_filter": {"enabled": True, "executable": True},
    }


def test_v462_trend_aligned_policy_uses_configured_version_label():
    candidate = _candidate("ALTUSDT", 0.95, 2.0)
    candidate["entry_type"] = "v3_momentum"

    policy = _regime_policy(
        candidate,
        {
            "opportunity_v4_strategy_version": "v4.6.2",
            "opportunity_v44_full_bet_enabled": True,
        },
    )

    assert policy["scope"] == "v44_trend_aligned_full_bet"
    assert policy["live_scope"] is True
    assert "V4.6.2" in policy["reason"]


def test_v462_non_admitted_position_confidence_uses_configured_version_label():
    confidence = v44_position_confidence(
        {"admission_lane": "shadow_only", "admitted": False},
        {"opportunity_v4_strategy_version": "v4.6.2"},
    )

    assert confidence["applied"] is False
    assert "V4.6.2" in confidence["reason"]


def test_v462_feature_weights_are_normalized_and_flow_led():
    assert sum(V462_FEATURE_WEIGHTS.values()) == 1.0
    assert V462_FEATURE_WEIGHTS["directed_flow"] == max(V462_FEATURE_WEIGHTS.values())
    assert V462_FEATURE_WEIGHTS["liquidity"] == min(V462_FEATURE_WEIGHTS.values())


def test_v411_weights_prioritize_regime_and_anti_chase():
    assert sum(V411_FEATURE_WEIGHTS.values()) == 1.0
    assert V411_FEATURE_WEIGHTS["regime_fit"] > V411_FEATURE_WEIGHTS["directed_flow"]
    assert V411_FEATURE_WEIGHTS["anti_chase"] > V411_FEATURE_WEIGHTS["volume_persistence"]


def test_v52_weights_prioritize_cross_sectional_strength_and_are_normalized():
    assert sum(V52_FEATURE_WEIGHTS.values()) == pytest.approx(1.0)
    assert V52_FEATURE_WEIGHTS["cross_sectional_strength"] == max(V52_FEATURE_WEIGHTS.values())
    assert strategy_supports("v5.2", "episode_evidence") is True
    assert strategy_supports("v5.2", "hard_stop_headroom") is True


def test_v53_fusion_weights_and_capabilities_are_isolated():
    assert sum(V53_FEATURE_WEIGHTS.values()) == pytest.approx(1.0)
    assert V53_FEATURE_WEIGHTS["cross_sectional_strength"] == 0.25
    assert V53_FEATURE_WEIGHTS["medium_path"] == 0.20
    assert V53_FEATURE_WEIGHTS["entry_quality"] == 0.20
    assert strategy_supports("v5.3", "v53_fusion") is True
    assert strategy_supports("v5.3", "episode_evidence") is True
    assert strategy_supports("v5.3", "hard_stop_headroom") is True
    assert strategy_supports("v5.2", "v53_fusion") is False
    assert strategy_supports("v5.4", "v54_history_router") is True


def test_v53_routes_direction_with_market_regime():
    long_candidate = _candidate("LONGUSDT", 0.95, 2.0)
    short_candidate = _candidate("SHORTUSDT", 0.95, 2.0)
    short_candidate["direction"] = "SHORT"
    short_candidate["signal"]["signal"] = "SHORT"
    short_candidate["market_state"]["state"] = "broad_down"
    short_candidate["opportunity_v3"]["market_regime"] = "broad_down"

    config = {
        "opportunity_v4_strategy_version": "v5.3",
        "opportunity_v44_full_bet_enabled": True,
    }

    assert _regime_policy(long_candidate, config)["scope"] == "v53_broad_up_trend_route"
    assert _regime_policy(short_candidate, config)["scope"] == "v53_broad_down_trend_route"


def test_v54_history_router_only_admits_supported_pullback_contexts():
    short_candidate = _candidate("SHORTUSDT", 0.95, 2.0)
    short_candidate["direction"] = "SHORT"
    short_candidate["signal"]["signal"] = "SHORT"
    short_candidate["entry_type"] = "v3_pullback"
    short_candidate["market_state"]["state"] = "broad_down"
    short_candidate["opportunity_v3"]["market_regime"] = "broad_down"
    config = {
        "opportunity_v4_strategy_version": "v5.4",
        "opportunity_v44_full_bet_enabled": True,
        "opportunity_v54_core_max_risk_pct": 15.0,
    }

    admitted = _regime_policy(short_candidate, config)
    assert admitted["scope"] == "v54_broad_down_short_pullback_core"
    assert admitted["live_scope"] is True
    assert admitted["risk_cap_pct"] == 15.0

    rejected = _candidate("LONGUSDT", 0.95, 2.0)
    rejected["entry_type"] = "v3_pullback"
    assert _regime_policy(rejected, config)["scope"] == "v54_history_shadow_only"


def test_v54_position_confidence_respects_history_route_risk_cap():
    confidence = v44_position_confidence(
        {
            "admission_lane": "full_bet",
            "admitted": True,
            "rank_percentile": 1.0,
            "v44_confirmations": 5,
            "cost_ratio": 12.0,
            "lower_expected_net_pct": 1.0,
            "liquidity_gate": {"passed": True},
            "direction": "SHORT",
            "v54_history_router": {"risk_cap_pct": 15.0},
        },
        {
            "opportunity_v4_strategy_version": "v5.4",
            "opportunity_v50_min_risk_pct": 12.0,
            "opportunity_v50_max_risk_pct": 30.0,
            "opportunity_v50_stressed_risk_cap_pct": 30.0,
            "opportunity_v54_core_rank_percentile": 0.80,
            "opportunity_v54_min_confirmations": 2,
            "opportunity_v54_min_gross_cost_multiple": 3.50,
        },
    )

    assert confidence["method"] == "s0_full_bet_v54_history_router"
    assert confidence["target_initial_risk_pct"] <= 15.0


def test_v53_uses_quieter_market_rank_floor_and_current_schema(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    config = {
        "opportunity_v4_strategy_version": "v5.3",
        "opportunity_v4_live_enabled": True,
        "opportunity_v44_full_bet_enabled": True,
        "opportunity_v50_min_quality_score": 0.0,
        "opportunity_v50_min_expected_net_pct": -10.0,
        "opportunity_v50_min_lower_expectancy_pct": -10.0,
        "opportunity_v53_min_rank_percentile": 0.0,
        "opportunity_v53_quiet_min_rank_percentile": 0.90,
        "opportunity_v53_min_cross_sectional_strength": 0.0,
        "opportunity_v53_min_gross_cost_multiple": 1.0,
        "opportunity_v53_min_confirmations": 2,
        "opportunity_v48_exhaustion_enabled": False,
        "opportunity_v48_reentry_enabled": False,
        "opportunity_v48_local_evidence_enabled": False,
        "opportunity_v53_adaptive_enabled": False,
    }
    candidate = _candidate("QUIETUSDT", 0.99, 2.0)
    candidate["market_state"]["state"] = "quiet"
    candidate["opportunity_v3"]["market_regime"] = "quiet"
    candidate["entry_type"] = "v3_pullback"

    attach_v4_rankings([candidate], config)
    opportunity = candidate["opportunity_v4"]

    assert opportunity["feature_schema_version"] == "v5.3"
    assert opportunity["v53_fusion"]["enabled"] is True
    assert opportunity["v53_fusion"]["rank_floor"] == pytest.approx(0.90)
    assert opportunity["admitted"] is True


def test_v52_evidence_compresses_repeated_opportunities_into_market_episodes(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    closes = (base, base + timedelta(minutes=10), base + timedelta(minutes=50))
    with connect() as conn:
        ensure_shadow_tables(conn)
        for index, closed_at in enumerate(closes):
            conn.execute(
                """
                INSERT INTO shadow_trades (
                    dedupe_key, opened_at, closed_at, symbol, direction, signal_type, status,
                    entry, stop, take_profit, last_price, notional, net_pnl, estimated_cost,
                    expires_at, strategy_family, strategy_version, strategy_role,
                    opportunity_id, market_regime, evidence_type, payload
                ) VALUES (?, ?, ?, 'ALTUSDT', 'LONG', 'v3_breakout', 'CLOSED',
                          100, 99, 103, 103, 20, 0.4, 0.02, ?,
                          'extreme_v5_roll', 'v5.2', 'active', ?, 'broad_up',
                          'decision', '{"features":{"entry_phase":"RETEST"}}')
                """,
                (
                    f"v52-episode-{index}",
                    (closed_at - timedelta(minutes=5)).isoformat(),
                    closed_at.isoformat(),
                    (closed_at + timedelta(hours=1)).isoformat(),
                    f"v52-opportunity-{index}",
                ),
            )
        conn.commit()

    rows = _load_evidence(
        {
            "opportunity_v4_strategy_version": "v5.2",
            "opportunity_v4_evidence_lookback_hours": 24,
            "opportunity_v52_episode_minutes": 30,
        }
    )

    assert len(rows) == 2
    assert rows[0]["episode_raw_opportunities"] == 2
    assert rows[1]["episode_raw_opportunities"] == 1


def test_v52_blocks_momentum_but_admits_strong_cost_covered_breakout(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    config = {
        "opportunity_v4_strategy_version": "v5.2",
        "opportunity_v4_live_enabled": True,
        "opportunity_v44_full_bet_enabled": True,
        "opportunity_v50_min_rank_percentile": 0.0,
        "opportunity_v50_min_quality_score": 0.0,
        "opportunity_v50_min_expected_net_pct": -10.0,
        "opportunity_v50_min_lower_expectancy_pct": -10.0,
        "opportunity_v50_min_cost_ratio": 0.0,
        "opportunity_v50_min_confirmations": 1,
        "opportunity_v51_breakout_min_rank_percentile": 0.0,
        "opportunity_v51_breakout_min_cost_ratio": 0.0,
        "opportunity_v51_breakout_min_confirmations": 1,
        "opportunity_v52_min_cross_sectional_strength": 0.78,
        "opportunity_v52_min_gross_cost_multiple": 3.5,
        "opportunity_v48_exhaustion_enabled": False,
        "opportunity_v48_reentry_enabled": False,
        "opportunity_v48_local_evidence_enabled": False,
    }
    breakout = _candidate("BREAKUSDT", 0.99, 2.0)
    momentum = _candidate("MOMUSDT", 0.99, 2.0)
    momentum["entry_type"] = "v3_momentum"

    attach_v4_rankings([breakout, momentum], config)

    assert breakout["opportunity_v4"]["admitted"] is True
    assert breakout["opportunity_v4"]["feature_schema_version"] == "v5.2"
    assert breakout["opportunity_v4"]["position_confidence"]["target_initial_risk_pct"] == 30.0
    assert momentum["opportunity_v4"]["admitted"] is False
    assert "纯动量" in "；".join(momentum["opportunity_v4"]["blockers"])


def test_v411_continuation_shape_penalizes_terminal_spikes():
    assert _continuation_shape(1.35, 0.75, 1.35, 4.50) == 1.0
    assert _continuation_shape(3.5, 0.75, 1.35, 4.50) < 1.0
    assert _continuation_shape(5.0, 0.75, 1.35, 4.50) == 0.0


def test_v472_inherits_execution_safeguards_but_uses_new_router():
    assert strategy_supports("v4.7.2", "full_bet") is True
    assert strategy_supports("v4.7.2", "continuous_permit") is True
    assert strategy_supports("v4.7.2", "global_market") is True
    assert strategy_supports("v4.7.2", "healthy_continuation") is True
    assert strategy_supports("v4.7.3", "full_bet") is True
    assert strategy_supports("v4.7.3", "v472_router") is True
    assert strategy_supports("v4.7.4", "full_bet") is True
    assert strategy_supports("v4.7.4", "v472_router") is True
    assert strategy_supports("v4.11", "v472_router") is False
    assert strategy_supports("v5.0-s30", "full_bet") is True
    assert strategy_supports("v5.0-s30", "continuous_permit") is True
    assert strategy_supports("v5.0-s30", "global_adaptive") is False

    pullback = _candidate("PULLUSDT", 0.95, 2.0)
    pullback["entry_type"] = "v3_pullback"
    momentum = _candidate("MOMUSDT", 0.95, 2.0)
    momentum["entry_type"] = "v3_momentum"
    config = {
        "opportunity_v4_strategy_version": "v4.7.2",
        "opportunity_v44_full_bet_enabled": True,
    }

    assert _regime_policy(pullback, config)["scope"] == "v44_trend_aligned_full_bet"
    assert _regime_policy(momentum, config)["scope"] != "v44_trend_aligned_full_bet"


def test_v472_setup_adjustments_prefer_pullback():
    pullback = _candidate("PULLUSDT", 0.95, 2.0)
    pullback["entry_type"] = "v3_pullback"
    momentum = _candidate("MOMUSDT", 0.95, 2.0)
    momentum["entry_type"] = "v3_momentum"
    config = {"opportunity_v4_strategy_version": "v4.7.2"}

    pullback_model = _model_expectancy(pullback, _model_features(pullback, config), config)
    momentum_model = _model_expectancy(momentum, _model_features(momentum, config), config)

    assert pullback_model["setup_adjustment"] == 0.06
    assert momentum_model["setup_adjustment"] == -0.08


def test_v473_uses_fast_pullback_protection_profile():
    candidate = _candidate("FASTUSDT", 0.95, 2.0)
    candidate["entry_type"] = "v3_pullback"
    profile = _protection_profile(
        candidate,
        {
            "opportunity_v4_strategy_version": "v4.7.3",
            "opportunity_v44_full_bet_enabled": True,
            "opportunity_v473_stagnation_seconds": 180,
            "opportunity_v473_stagnation_min_profit_pct": 0.12,
            "opportunity_v473_max_hold_seconds": 480,
            "opportunity_v473_fast_invalid_seconds": 120,
        },
    )

    assert profile["profile"] == "s0_full_bet_v473"
    assert profile["stagnation_seconds"] == 180
    assert profile["stagnation_min_profit_pct"] == 0.12
    assert profile["max_hold_seconds"] == 480
    assert profile["fast_invalid_seconds"] == 120


def test_v47_full_bet_uses_adaptive_calibration_and_keeps_risk_cap(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    candidate = _candidate("V47LONGUSDT", 0.95, 2.0)
    candidate["entry_type"] = "v3_momentum"
    config = {
        "opportunity_v4_strategy_version": "v4.7",
        "opportunity_v4_live_enabled": True,
        "opportunity_v44_full_bet_enabled": True,
        "opportunity_v44_min_rank_percentile": 0.80,
        "opportunity_v44_min_quality_score": 52.0,
        "opportunity_v44_min_expected_net_pct": 0.02,
        "opportunity_v44_min_lower_expectancy_pct": -0.05,
        "opportunity_v44_min_cost_ratio": 1.50,
        "opportunity_v44_min_confirmations": 3,
        "opportunity_v44_min_risk_pct": 8.0,
        "opportunity_v44_max_risk_pct": 15.0,
        "opportunity_v44_stressed_risk_cap_pct": 15.0,
    }

    attach_v4_rankings([candidate], config)
    opportunity = candidate["opportunity_v4"]

    assert opportunity["strategy_version"] == "v4.7"
    assert opportunity["adaptive_calibration"]["relation"] == "aligned"
    assert opportunity["position_confidence"]["target_initial_risk_pct"] <= 15.0


def test_v48_requires_absolute_quality_even_when_candidate_ranks_first(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    candidate = _candidate("WEAKTOPUSDT", 0.95, 2.0)
    config = {
        "opportunity_v4_strategy_version": "v4.8",
        "opportunity_v4_live_enabled": True,
        "opportunity_v44_full_bet_enabled": True,
        "opportunity_v44_min_rank_percentile": 0.80,
        "opportunity_v48_min_quality_score": 95.0,
        "opportunity_v48_min_expected_net_pct": -1.0,
        "opportunity_v48_min_lower_expectancy_pct": -1.0,
        "opportunity_v48_min_cost_ratio": 0.1,
        "opportunity_v44_min_confirmations": 2,
    }

    attach_v4_rankings([candidate], config)

    v4 = candidate["opportunity_v4"]
    assert v4["rank_percentile"] == 1.0
    assert v4["full_bet_admitted"] is False
    assert v4["score"] < 95.0


def test_v48_exhaustion_blocks_late_chase(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    candidate = _candidate("LATEUSDT", 0.99, 0.50)
    candidate["signal"].update(
        {"breakout_extension_atr": 2.0, "impulse_atr": 2.5, "adverse_wick_ratio": 3.0}
    )
    config = {
        "opportunity_v4_strategy_version": "v4.8",
        "opportunity_v4_live_enabled": True,
        "opportunity_v44_full_bet_enabled": True,
        "opportunity_v44_min_rank_percentile": 0.0,
        "opportunity_v48_min_quality_score": 0.0,
        "opportunity_v48_min_expected_net_pct": -1.0,
        "opportunity_v48_min_lower_expectancy_pct": -1.0,
        "opportunity_v48_min_cost_ratio": 0.0,
        "opportunity_v44_min_confirmations": 2,
    }

    attach_v4_rankings([candidate], config)

    v4 = candidate["opportunity_v4"]
    assert v4["exhaustion"]["blocked"] is True
    assert v4["full_bet_admitted"] is False


def test_v48_reentry_requires_new_structure_after_two_losses():
    candidate = _candidate("REENTERUSDT", 0.95, 1.0)
    features = _model_features(candidate)
    local = {"live_loss_streak": 2}
    config = {"opportunity_v48_reentry_enabled": True}

    blocked = _v48_reentry_policy(candidate, features, local, config)
    candidate["signal"].update(
        {"volume_acceleration": 1.20, "breakout_extension_atr": 0.20, "entry_phase": "RETEST"}
    )
    candidate["opportunity_v3"]["medium_path_efficiency"] = 0.40
    reset = _v48_reentry_policy(candidate, _model_features(candidate), local, config)

    assert blocked["blocked"] is True
    assert reset["blocked"] is False
    assert reset["structural_reset"] is True


def test_v472_reentry_blocks_duplicate_episode_without_two_losses():
    candidate = _candidate("DUPUSDT", 0.95, 1.0)
    candidate["signal"]["entry_phase"] = "TRIGGERED"
    local = {
        "live_loss_streak": 0,
        "episode": {
            "loss_streak": 0,
            "within_dedupe_window": True,
            "recent_event_count": 1,
            "recent_event_limit": 3,
            "dedupe_minutes": 45,
        },
    }

    result = _v48_reentry_policy(
        candidate,
        _model_features(candidate, {"opportunity_v4_strategy_version": "v4.7.2"}),
        local,
        {"opportunity_v48_reentry_enabled": True},
    )

    assert result["blocked"] is True
    assert result["duplicate_event"] is True


def test_v511_reentry_uses_same_direction_incident_guard():
    candidate = _candidate("ZILUSDT", 0.95, 1.0)
    candidate["signal"]["entry_phase"] = "TRIGGERED"
    local = {
        "live_loss_streak": 0,
        "episode": {
            "loss_streak": 1,
            "within_dedupe_window": True,
            "recent_event_count": 1,
            "recent_event_limit": 2,
            "dedupe_minutes": 120,
        },
    }
    config = {
        "opportunity_v4_strategy_version": "v5.1.1",
        "opportunity_v48_reentry_enabled": True,
        "opportunity_v511_reentry_caution_multiplier": 0.50,
    }

    result = _v48_reentry_policy(
        candidate,
        _model_features(candidate, config),
        local,
        config,
    )

    assert result["blocked"] is True
    assert result["duplicate_event"] is True
    assert result["dedupe_minutes"] == 120


def test_v53_reentry_does_not_reset_two_recent_same_direction_losses():
    candidate = _candidate("BEATUSDT", 0.95, 1.0)
    candidate["signal"].update(
        {"volume_acceleration": 1.20, "breakout_extension_atr": 0.20, "entry_phase": "RETEST"}
    )
    candidate["opportunity_v3"]["medium_path_efficiency"] = 0.40
    config = {
        "opportunity_v4_strategy_version": "v5.3",
        "opportunity_v48_reentry_enabled": True,
        "opportunity_v511_same_direction_hard_losses": 2,
    }
    local = {
        "live_loss_streak": 0,
        "episode": {
            "loss_streak": 2,
            "recent_loss_count": 2,
            "within_dedupe_window": False,
            "recent_event_count": 2,
            "recent_event_limit": 3,
        },
    }

    result = _v48_reentry_policy(candidate, _model_features(candidate, config), local, config)

    assert result["structural_reset"] is True
    assert result["hard_recent_losses"] is True
    assert result["blocked"] is True


def test_v462_rewards_momentum_and_penalizes_pullback_before_smart_flow():
    momentum = _candidate("MOMENTUMUSDT", 0.90, 2.0)
    momentum["entry_type"] = "v3_momentum"
    pullback = _candidate("PULLBACKUSDT", 0.90, 2.0)
    pullback["entry_type"] = "v3_pullback"

    momentum_model = _model_expectancy(momentum, _model_features(momentum), {})
    pullback_model = _model_expectancy(pullback, _model_features(pullback), {})

    assert momentum_model["setup_adjustment"] == 0.04
    assert pullback_model["setup_adjustment"] == -0.03
    assert momentum_model["quality"] > pullback_model["quality"]


def test_v462_short_position_confidence_applies_direction_risk_discount():
    base = {
        "admission_lane": "full_bet",
        "admitted": True,
        "rank_percentile": 1.0,
        "v44_confirmations": 5,
        "cost_ratio": 8.0,
        "lower_expected_net_pct": 0.15,
        "liquidity_gate": {"passed": True},
    }
    config = {
        "opportunity_v4_strategy_version": "v4.6.2",
        "opportunity_v44_min_rank_percentile": 0.8,
        "opportunity_v44_min_confirmations": 3,
        "opportunity_v44_min_cost_ratio": 1.5,
        "opportunity_v44_min_lower_expectancy_pct": -0.05,
        "opportunity_v44_min_risk_pct": 8.0,
        "opportunity_v44_max_risk_pct": 15.0,
        "opportunity_v462_short_risk_multiplier": 0.65,
    }

    long_result = v44_position_confidence({**base, "direction": "LONG"}, config)
    short_result = v44_position_confidence({**base, "direction": "SHORT"}, config)

    assert short_result["target_initial_risk_pct"] == long_result["target_initial_risk_pct"] * 0.65
    assert short_result["components"]["direction_risk"] == 0.65


def test_v462_short_full_bet_requires_stronger_directional_flow(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    clear_v4_evidence_cache()
    config = {
        "opportunity_v4_strategy_version": "v4.6.2",
        "opportunity_v4_live_enabled": True,
        "opportunity_v44_full_bet_enabled": True,
        "opportunity_v44_min_rank_percentile": 0.8,
        "opportunity_v44_min_quality_score": 52.0,
        "opportunity_v44_min_expected_net_pct": 0.02,
        "opportunity_v44_min_lower_expectancy_pct": -0.05,
        "opportunity_v44_min_cost_ratio": 1.5,
        "opportunity_v44_min_confirmations": 3,
        "opportunity_v462_short_min_directed_flow": 0.72,
        "opportunity_v462_short_min_regime_fit": 0.85,
        "opportunity_v462_short_min_medium_path": 0.45,
    }

    weak = _candidate("WEAKSHORTUSDT", 0.95, 2.0)
    weak.update({"direction": "SHORT", "entry_type": "v3_momentum"})
    weak["signal"].update({"signal": "SHORT", "directed_trade_flow": 0.50})
    weak["opportunity_v3"].update({"market_regime": "broad_down", "medium_trend_aligned": True})
    attach_v4_rankings([weak], config)

    strong = _candidate("STRONGSHORTUSDT", 0.95, 2.0)
    strong.update({"direction": "SHORT", "entry_type": "v3_momentum"})
    strong["signal"].update({"signal": "SHORT", "directed_trade_flow": 0.62})
    strong["opportunity_v3"].update({"market_regime": "broad_down", "medium_trend_aligned": True})
    attach_v4_rankings([strong], config)

    assert weak["opportunity_v4"]["full_bet_admitted"] is False
    assert any("做空需要更强" in reason for reason in weak["opportunity_v4"]["blockers"])
    assert strong["opportunity_v4"]["full_bet_admitted"] is True


def test_v4_ranks_net_expectancy_but_does_not_promote_when_bootstrap_is_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    clear_v4_evidence_cache()
    weak = _candidate("WEAKUSDT", 0.55, 1.0)
    weak["execution_filter"] = {"enabled": True, "executable": False}
    strong = _candidate("STRONGUSDT", 0.95, 2.0)

    attach_v4_rankings(
        [weak, strong],
        {"opportunity_v4_live_enabled": True, "opportunity_v4_bootstrap_enabled": False},
    )

    assert strong["opportunity_v4"]["rank_percentile"] > weak["opportunity_v4"]["rank_percentile"]
    assert strong["opportunity_v4"]["lower_expected_net_pct"] > weak["opportunity_v4"]["lower_expected_net_pct"]
    assert strong["opportunity_v4"]["admitted"] is False
    assert strong["opportunity_v4"]["evidence_status"] == "collecting"
    assert weak["opportunity_v4"]["shadow_eligible"] is True


def test_v4_bootstrap_admits_only_top_executable_candidate(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    clear_v4_evidence_cache()
    weak = _candidate("WEAKUSDT", 0.55, 1.0)
    strong = _candidate("STRONGUSDT", 0.95, 2.0)

    attach_v4_rankings([weak, strong], {"opportunity_v4_live_enabled": True})

    assert strong["opportunity_v4"]["admitted"] is True
    assert strong["opportunity_v4"]["bootstrap_admitted"] is True
    assert strong["opportunity_v4"]["evidence_status"] == "canary"
    assert strong["opportunity_v4"]["risk_multiplier"] == 0.4
    assert weak["opportunity_v4"]["admitted"] is False


def test_v4_live_selector_ignores_v3_tier_and_applies_admission_risk_once():
    candidate = _candidate("ALTUSDT", 0.95, 2.0)
    candidate.update(
        {
            "base_risk_pct": 10.0,
            "risk_pct": 0.0,
            "passed": False,
            "symbol_quality": {"allowed": False, "tier": "WATCH", "score": 20},
            "opportunity_v4": {
                "admitted": True,
                "bootstrap_admitted": True,
                "validated": False,
                "risk_multiplier": 0.4,
                "strategy_version": "v4.3",
                "score": 75,
                "rank_percentile": 0.95,
                "evidence_status": "bootstrap",
                "reason": "V4 顶排候选进入受限实盘探索",
            },
        }
    )

    selected = _apply_v4_live_selection(
        candidate,
        {"opportunity_v4_enabled": True, "opportunity_v4_live_enabled": True},
        {"risk_pct": 10.0},
    )
    effective = effective_position_risk(
        candidate_risk_pct=selected["risk_pct"],
        candidate=selected,
        guard={"risk_multiplier": 1.0},
        target={"effective_risk_multiplier": 1.0},
        config={"effective_position_sizing_enabled": True},
        mode="extreme_sprint",
    )

    assert selected["passed"] is True
    assert selected["strategy_family"] == "extreme_v4_roll"
    assert "legacy_v3_quality" not in selected
    assert selected["symbol_quality"]["tier"] == "V4.3-CORE-CANARY"
    assert selected["risk_pct"] == 4.0
    assert selected["quality_risk_multiplier"] == 1.0
    assert selected["risk_adjustment"]["multiplier"] == 1.0
    assert effective["final_risk_pct"] == 4.0


def test_v43_candidate_admission_normalizes_setup_aliases(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    config = {
        "opportunity_v4_live_enabled": True,
        "opportunity_v4_strategy_version": "v4.3",
        "opportunity_v4_decision_min_rank_percentile": 0,
        "opportunity_v4_admission_min_trades": 10,
        "opportunity_v4_admission_min_profit_factor": 1.1,
        "opportunity_v4_admission_min_lower_expectancy_pct": 0.0,
        "opportunity_v41_validation_min_trades": 10,
        "opportunity_v41_validation_min_profit_factor": 1.1,
        "opportunity_v41_validation_min_time_blocks": 1,
        "opportunity_v41_validation_min_symbols": 1,
        "opportunity_v4_evidence_lookback_hours": 10_000,
    }
    with connect() as conn:
        ensure_shadow_tables(conn)
        for index in range(12):
            net = 0.3 if index < 10 else -0.1
            conn.execute(
                """
                INSERT INTO shadow_trades (
                    dedupe_key, opened_at, closed_at, symbol, direction, signal_type, status,
                    entry, stop, take_profit, last_price, notional, net_pnl, expires_at,
                    strategy_family, strategy_version, strategy_role, opportunity_id,
                    market_regime, evidence_type, payload
                ) VALUES (?, '2026-07-01T00:00:00+00:00', '2026-07-01T01:00:00+00:00',
                          'ALTUSDT', 'LONG', 'pullback', 'CLOSED', 100, 99, 103, 103,
                          20, ?, '2026-07-01T02:00:00+00:00', 'extreme_v4_roll',
                          'v4.3', 'active', ?, 'quiet', 'decision',
                          '{"features":{"entry_phase":"RETEST","medium_trend_aligned":true}}')
                """,
                (f"v4-{index}", net, f"op-{index}"),
            )
        conn.commit()
    clear_v4_evidence_cache()
    candidate = _candidate("ALTUSDT", 0.9, 1.8)
    candidate["entry_type"] = "v3_pullback"
    candidate["market_state"] = {"state": "quiet"}
    candidate["opportunity_v3"]["market_regime"] = "quiet"

    attach_v4_rankings([candidate], config)

    evidence = candidate["opportunity_v4"]
    assert evidence["evidence"]["selected"]["trades"] == 12
    assert evidence["evidence"]["scope"] == "regime_direction_setup_phase"
    assert evidence["admitted"] is True
    assert evidence["evidence_status"] == "validated"


def test_v41_primary_evidence_excludes_exploration_and_dedupes_opportunity(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    config = {
        "opportunity_v4_strategy_version": "v4.1",
        "opportunity_v4_evidence_lookback_hours": 10_000,
    }
    with connect() as conn:
        ensure_shadow_tables(conn)
        for index, evidence_type in enumerate(("decision", "decision", "exploration")):
            conn.execute(
                """
                INSERT INTO shadow_trades (
                    dedupe_key, opened_at, closed_at, symbol, direction, signal_type, status,
                    entry, stop, take_profit, last_price, notional, net_pnl, estimated_cost, expires_at,
                    strategy_family, strategy_version, strategy_role, opportunity_id,
                    market_regime, evidence_type, payload
                ) VALUES (?, '2026-07-16T00:00:00+00:00', '2026-07-16T01:00:00+00:00',
                          'ALTUSDT', 'SHORT', 'v3_pullback', 'CLOSED', 100, 101, 97, 97,
                          20, ?, 0.02, '2026-07-16T02:00:00+00:00', 'extreme_v4_roll',
                          'v4.1', 'active', 'same-opportunity', 'quiet', ?,
                          '{"features":{"entry_phase":"RETEST","medium_trend_aligned":true}}')
                """,
                (f"evidence-{index}", 0.4 if evidence_type == "decision" else -5, evidence_type),
            )
        conn.commit()
    clear_v4_evidence_cache()
    candidate = _candidate("ALTUSDT", 0.95, 2.0)
    candidate["direction"] = "SHORT"
    candidate["entry_type"] = "v3_pullback"
    candidate["signal"].update({"signal": "SHORT", "stop": 101, "take_profit": 97})
    candidate["market_state"] = {"state": "quiet"}
    candidate["opportunity_v3"]["market_regime"] = "quiet"

    attach_v4_rankings([candidate], config)

    selected = candidate["opportunity_v4"]["evidence"]["selected"]
    assert selected["trades"] == 1
    assert selected["net_pct"] > 0


def test_v41_retest_scores_above_triggered_and_dynamic_cost_can_block(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    clear_v4_evidence_cache()
    retest = _candidate("RETESTUSDT", 0.9, 2.0)
    triggered = _candidate("TRIGGEREDUSDT", 0.9, 2.0)
    triggered["signal"]["entry_phase"] = "TRIGGERED"
    expensive = _candidate("EXPENSIVEUSDT", 0.99, 2.5)
    expensive["estimated_cost_pct"] = 2.0

    attach_v4_rankings([retest, triggered, expensive], {"opportunity_v4_live_enabled": True})

    assert retest["opportunity_v4"]["score"] > triggered["opportunity_v4"]["score"]
    assert expensive["opportunity_v4"]["admitted"] is False
    assert any("成本比" in reason for reason in expensive["opportunity_v4"]["blockers"])


def _momentum_candidate(symbol: str, direction: str, regime: str) -> dict:
    candidate = _candidate(symbol, 0.95, 2.2)
    candidate["direction"] = direction
    candidate["entry_type"] = "v3_momentum"
    candidate["signal"].update(
        {
            "signal": direction,
            "stop": 101 if direction == "SHORT" else 99,
            "take_profit": 97 if direction == "SHORT" else 103,
            "entry_phase": "ARMED",
            "directed_trade_flow": 0.65,
        }
    )
    candidate["market_state"] = {"state": regime}
    candidate["opportunity_v3"].update(
        {
            "market_regime": regime,
            "medium_trend_aligned": False,
            "medium_path_efficiency": 0.25,
        }
    )
    return candidate


def test_v43_broad_down_short_momentum_waits_for_new_shadow_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    clear_v4_evidence_cache()
    candidate = _momentum_candidate("DOWNUSDT", "SHORT", "broad_down")

    attach_v4_rankings([candidate], {"opportunity_v4_live_enabled": True})

    v4 = candidate["opportunity_v4"]
    assert v4["admitted"] is False
    assert v4["exploration_admitted"] is False
    assert v4["admission_lane"] == "shadow_only"
    assert v4["risk_multiplier"] == 0.0
    assert v4["canary_eligible"] is False
    assert v4["regime_policy"]["scope"] == "broad_down_short_shadow_only"


def test_v42_countertrend_and_panic_remain_shadow_only(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    clear_v4_evidence_cache()
    countertrend = _momentum_candidate("COUNTERUSDT", "LONG", "broad_down")
    panic = _momentum_candidate("PANICUSDT", "SHORT", "panic")

    attach_v4_rankings([countertrend, panic], {"opportunity_v4_live_enabled": True})

    assert countertrend["opportunity_v4"]["admitted"] is False
    assert countertrend["opportunity_v4"]["admission_lane"] == "shadow_only"
    assert panic["opportunity_v4"]["admitted"] is False
    assert panic["opportunity_v4"]["regime_policy"]["scope"] == "panic_shadow_only"


def test_v51_panic_recovery_can_enter_candidate_scope(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    clear_v4_evidence_cache()
    candidate = _candidate("RECOVERYUSDT", 0.98, 3.2)
    candidate["direction"] = "LONG"
    candidate["entry_type"] = "v3_pullback"
    candidate["signal"].update(
        {
            "signal": "LONG",
            "entry_phase": "RETEST",
            "volume_acceleration": 1.20,
            "directed_trade_flow": 0.72,
            "breakout_extension_atr": 0.20,
        }
    )
    candidate["market_state"] = {"state": "panic"}
    candidate["opportunity_v3"].update(
        {
            "market_regime": "panic",
            "medium_trend_aligned": True,
            "medium_path_efficiency": 0.45,
        }
    )

    attach_v4_rankings(
        [candidate],
        {
            "opportunity_v4_live_enabled": True,
            "opportunity_v4_strategy_version": "v5.1",
        },
    )

    policy = candidate["opportunity_v4"]["regime_policy"]
    assert policy["scope"] == "panic_recovery_candidate"
    assert policy["market_phase"] == "panic_recovery"
    assert policy["live_scope"] is True
    assert policy["recovery_confirmations"] == 7


def test_v43_broad_down_short_pullback_stays_shadow_after_negative_replay(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    clear_v4_evidence_cache()
    candidate = _candidate("PULLBACKUSDT", 0.95, 2.0)
    candidate["direction"] = "SHORT"
    candidate["entry_type"] = "v3_pullback"
    candidate["signal"].update({"signal": "SHORT", "stop": 101, "take_profit": 97, "entry_phase": "RETEST"})
    candidate["market_state"] = {"state": "broad_down"}
    candidate["opportunity_v3"].update({"market_regime": "broad_down", "medium_trend_aligned": False})

    attach_v4_rankings([candidate], {"opportunity_v4_live_enabled": True})

    v4 = candidate["opportunity_v4"]
    assert v4["regime_policy"]["scope"] == "broad_down_short_shadow_only"
    assert v4["bootstrap_admitted"] is False
    assert v4["admission_lane"] == "shadow_only"


def test_v43_dynamic_liquidity_uses_expected_probe_notional(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    clear_v4_evidence_cache()
    candidate = _candidate("SHALLOWUSDT", 0.95, 2.0)
    candidate["depth"] = {"spread_pct": 0.02, "depth_notional": 1_000}
    candidate["execution_filter"] = {
        "enabled": True,
        "executable": True,
        "raw_quantity": 1.0,
        "max_quantity": 1.0,
        "notional": 100.0,
        "min_notional": 5.0,
    }

    attach_v4_rankings([candidate], {"opportunity_v4_live_enabled": True})

    gate = candidate["opportunity_v4"]["liquidity_gate"]
    assert gate["estimated_order_notional"] == 40.0
    assert gate["required_depth_notional"] == 750.0
    assert gate["passed"] is True
    assert candidate["opportunity_v4"]["bootstrap_admitted"] is True


def test_v43_direction_losses_reduce_risk_without_vetoing_new_local_setup(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    config = {
        "opportunity_v4_live_enabled": True,
        "opportunity_v4_strategy_version": "v4.3",
        "opportunity_v4_evidence_lookback_hours": 10_000,
    }
    with connect() as conn:
        ensure_shadow_tables(conn)
        for index in range(20):
            conn.execute(
                """
                INSERT INTO shadow_trades (
                    dedupe_key, opened_at, closed_at, symbol, direction, signal_type, status,
                    entry, stop, take_profit, last_price, notional, net_pnl, expires_at,
                    strategy_family, strategy_version, strategy_role, opportunity_id,
                    market_regime, evidence_type, payload
                ) VALUES (?, '2026-07-17T00:00:00+00:00', '2026-07-17T01:00:00+00:00',
                          ?, 'LONG', 'momentum', 'CLOSED', 100, 99, 103, 99,
                          20, -0.2, '2026-07-17T02:00:00+00:00', 'extreme_v4_roll',
                          'v4.3', 'active', ?, 'mixed', 'decision',
                          '{"features":{"entry_phase":"ARMED"}}')
                """,
                (f"direction-loss-{index}", f"LOSS{index}USDT", f"direction-op-{index}"),
            )
        conn.commit()
    clear_v4_evidence_cache()
    candidate = _candidate("NEWSETUPUSDT", 0.95, 2.0)

    attach_v4_rankings([candidate], config)

    v4 = candidate["opportunity_v4"]
    assert v4["evidence"]["scope"] == "model_only"
    assert v4["evidence_risk_multiplier"] == 0.55
    assert v4["evidence_status"] == "canary"
    assert v4["admitted"] is True
    assert v4["risk_multiplier"] == 0.22


def test_v431_canary_cannot_bypass_negative_blended_expectancy(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    config = {
        "opportunity_v4_live_enabled": True,
        "opportunity_v4_strategy_version": "v4.3.1",
        "opportunity_v4_evidence_lookback_hours": 10_000,
        "opportunity_v431_local_block_min_trades": 8,
    }
    with connect() as conn:
        ensure_shadow_tables(conn)
        for index in range(7):
            conn.execute(
                """
                INSERT INTO shadow_trades (
                    dedupe_key, opened_at, closed_at, symbol, direction, signal_type, status,
                    entry, stop, take_profit, last_price, notional, net_pnl, expires_at,
                    strategy_family, strategy_version, strategy_role, opportunity_id,
                    market_regime, evidence_type, payload
                ) VALUES (?, '2026-07-18T00:00:00+00:00', '2026-07-18T01:00:00+00:00',
                          ?, 'LONG', 'pullback', 'CLOSED', 100, 99, 103, 99,
                          20, -10, '2026-07-18T02:00:00+00:00', 'extreme_v4_roll',
                          'v4.3.1', 'active', ?, 'broad_up', 'decision',
                          '{"admission_lane":"shadow_only","features":{"entry_phase":"RETEST"}}')
                """,
                (f"blend-loss-{index}", f"LOSS{index}USDT", f"blend-op-{index}"),
            )
        conn.commit()
    clear_v4_evidence_cache()
    candidate = _candidate("FRESHUSDT", 0.99, 2.5)
    candidate["entry_type"] = "pullback"

    attach_v4_rankings([candidate], config)

    v4 = candidate["opportunity_v4"]
    assert v4["model_expected_net_pct"] > 0
    assert v4["expected_net_pct"] < 0
    assert v4["local_circuit"]["blocked"] is False
    assert v4["bootstrap_admitted"] is False
    assert v4["admission_lane"] == "shadow_only"
