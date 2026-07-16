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
    assert strong["opportunity_v4"]["evidence_status"] == "bootstrap"
    assert strong["opportunity_v4"]["risk_multiplier"] == 0.5
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
                "risk_multiplier": 0.5,
                "strategy_version": "v4.0",
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
    assert selected["legacy_v3_quality"]["tier"] == "WATCH"
    assert selected["symbol_quality"]["tier"] == "V4-BOOTSTRAP"
    assert selected["risk_pct"] == 5.0
    assert selected["quality_risk_multiplier"] == 1.0
    assert selected["risk_adjustment"]["multiplier"] == 1.0
    assert effective["final_risk_pct"] == 5.0


def test_v4_candidate_admission_uses_its_own_cohort(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    config = {
        "opportunity_v4_live_enabled": True,
        "opportunity_v4_strategy_version": "v4.0",
        "opportunity_v4_decision_min_rank_percentile": 0,
        "opportunity_v4_admission_min_trades": 10,
        "opportunity_v4_admission_min_profit_factor": 1.1,
        "opportunity_v4_admission_min_lower_expectancy_pct": 0.02,
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
                          'ALTUSDT', 'LONG', 'v3_breakout', 'CLOSED', 100, 99, 103, 103,
                          20, ?, '2026-07-01T02:00:00+00:00', 'extreme_v4_roll',
                          'v4.0', 'active', ?, 'broad_up', 'decision', '{}')
                """,
                (f"v4-{index}", net, f"op-{index}"),
            )
        conn.commit()
    clear_v4_evidence_cache()
    candidate = _candidate("ALTUSDT", 0.9, 1.8)

    attach_v4_rankings([candidate], config)

    evidence = candidate["opportunity_v4"]
    assert evidence["evidence"]["selected"]["trades"] == 12
    assert evidence["admitted"] is True
    assert evidence["evidence_status"] == "validated"
