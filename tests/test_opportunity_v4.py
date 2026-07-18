from __future__ import annotations

from app.opportunity_v4 import attach_v4_rankings, clear_v4_evidence_cache
from app.position_sizing import effective_position_risk
from app.scanner import _apply_v4_live_selection
from app.shadow_trading import ensure_shadow_tables
from app.telemetry import connect


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
