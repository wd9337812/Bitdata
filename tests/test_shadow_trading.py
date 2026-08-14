from datetime import datetime, timedelta, timezone

from app.shadow_trading import record_execution_mirror, shadow_summary, update_shadow_trades
from app.telemetry import connect


def _candidate(price: float) -> dict:
    return {
        "symbol": "SOLUSDT",
        "direction": "LONG",
        "mode": "extreme_sprint",
        "entry_type": "trend_pullback",
        "score": 90,
        "passed": False,
        "decision_reason": "等待实盘条件",
        "signal": {"last_price": price, "stop": 95, "take_profit": 105},
    }


def test_shadow_trade_is_deduplicated_and_settled_without_exchange(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    config = {
        "shadow_trading_enabled": True,
        "shadow_min_candidate_score": 70,
        "shadow_dedupe_minutes": 10,
        "shadow_max_hold_minutes": 120,
        "shadow_reference_notional_usdt": 20,
        "shadow_round_trip_cost_pct": 0.12,
    }

    first = update_shadow_trades([_candidate(100)], config)
    duplicate = update_shadow_trades([_candidate(100)], config)
    settled = update_shadow_trades([_candidate(106)], config)
    summary = shadow_summary()

    assert first["opened"] == 1
    assert duplicate["opened"] == 0
    assert settled["closed"] == 1
    assert summary["stats"]["total"] == 1
    assert summary["stats"]["wins"] == 1
    assert summary["trades"][0]["outcome"] == "TAKE_PROFIT"


def test_execution_mirror_uses_final_live_opportunity_id(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    config = {"opportunity_v552_execution_mirror_enabled": True}
    decision = {
        "opportunity_id": "live-opportunity-1",
        "event_id": "event-1",
        "execution_id": "binance:123",
        "symbol": "SOLUSDT",
        "direction": "LONG",
        "quantity": 0.2,
        "candidate": {
            "symbol": "SOLUSDT",
            "direction": "LONG",
            "strategy_family": "extreme_v5_roll",
            "strategy_version": "v5.5.2",
            "strategy_role": "active",
            "entry_type": "pullback",
            "signal": {"last_price": 100, "stop": 98, "take_profit": 104},
            "opportunity_v4": {"score": 72, "admission_lane": "full_bet"},
        },
    }

    assert record_execution_mirror(
        decision,
        {"mode": "live", "entry_order": {"orderId": 123, "avgPrice": "100", "executedQty": "0.2"}},
        config,
    )
    assert not record_execution_mirror(
        decision,
        {"mode": "live", "entry_order": {"orderId": 123, "avgPrice": "100", "executedQty": "0.2"}},
        config,
    )
    with connect() as conn:
        row = dict(conn.execute("SELECT * FROM shadow_trades").fetchone())

    assert row["opportunity_id"] == "live-opportunity-1"
    assert row["evidence_type"] == "execution_mirror"
    assert row["notional"] == 20.0


def test_independent_research_shadow_can_force_eligibility_and_stay_single_position(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    candidate = {
        **_candidate(100),
        "symbol": "ALTUSDT",
        "score": 0,
        "strategy_family": "cross_sectional_momentum",
        "strategy_version": "s0_xmom_24h_v1",
        "strategy_role": "challenger",
        "evidence_type": "independent_realtime",
        "shadow_force_eligible": True,
        "shadow_single_position": True,
        "shadow_max_hold_minutes": 12 * 60,
        "shadow_dedupe_key": "s0_xmom_24h_v1:2026-07-31T08:00:00Z",
        "signal": {
            "signal": "LONG",
            "last_price": 100,
            "stop": 95,
            "take_profit": 109,
            "protection_profile": {"max_hold_seconds": 12 * 3600},
        },
    }
    config = {
        "shadow_trading_enabled": True,
        "shadow_min_candidate_score": 70,
        "shadow_max_hold_minutes": 120,
        "shadow_reference_notional_usdt": 20,
        "shadow_round_trip_cost_pct": 0.12,
    }

    first = update_shadow_trades([candidate], config)
    second = update_shadow_trades(
        [
            {
                **candidate,
                "symbol": "OTHERUSDT",
                "shadow_dedupe_key": "s0_xmom_24h_v1:2026-07-31T09:00:00Z",
            }
        ],
        config,
    )
    with connect() as conn:
        expires_at = conn.execute("SELECT expires_at FROM shadow_trades").fetchone()[0]

    assert first["opened"] == 1
    assert second["opened"] == 0
    assert datetime.fromisoformat(expires_at) > datetime.now(timezone.utc) + timedelta(hours=11)


def test_v473_shadow_uses_candidate_protection_for_stagnation_exit(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    candidate = {
        **_candidate(100),
        "strategy_family": "extreme_v4_roll",
        "strategy_version": "v4.7.3",
        "strategy_role": "active",
        "evidence_type": "decision",
        "opportunity_v4": {
            "shadow_eligible": True,
            "admitted": True,
            "score": 90,
            "protection_profile": {
                "max_hold_seconds": 480,
                "stagnation_seconds": 180,
                "stagnation_min_profit_pct": 0.12,
            },
        },
    }
    config = {
        "shadow_trading_enabled": True,
        "opportunity_v4_strategy_version": "v4.7.3",
        "shadow_dedupe_minutes": 10,
        "shadow_max_hold_minutes": 120,
        "shadow_reference_notional_usdt": 20,
        "shadow_round_trip_cost_pct": 0.12,
    }

    opened = update_shadow_trades([candidate], config)
    with connect() as conn:
        conn.execute(
            "UPDATE shadow_trades SET opened_at = ?",
            ((datetime.now(timezone.utc) - timedelta(seconds=240)).isoformat(),),
        )
        conn.commit()
    settled = update_shadow_trades(
        [{**candidate, "signal": {**candidate["signal"], "last_price": 100.05}}],
        config,
    )
    summary = shadow_summary(config=config)

    assert opened["opened"] == 1
    assert settled["closed"] == 1
    assert summary["trades"][0]["outcome"] == "STAGNATION_EXIT"


def test_v32_and_v33_paired_shadow_trades_can_coexist(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    base = {
        **_candidate(100),
        "entry_type": "v3_breakout",
        "signal": {"signal": "LONG", "last_price": 100, "stop": 95, "take_profit": 105},
    }
    v3 = {
        **base,
        "strategy_family": "extreme_v3_roll",
        "strategy_version": "v3.2",
        "strategy_role": "active",
        "opportunity_v3": {"eligible": True, "score": 80},
    }
    v33 = {
        **base,
        "strategy_family": "extreme_v3_roll",
        "strategy_version": "v3.3-candidate",
        "strategy_role": "challenger",
        "opportunity_v33": {"eligible": True, "score": 82},
    }
    config = {
        "shadow_trading_enabled": True,
        "shadow_min_candidate_score": 70,
        "opportunity_v33_min_score": 68,
        "opportunity_v3_strategy_version": "v3.2",
        "opportunity_v33_strategy_version": "v3.3-candidate",
        "shadow_dedupe_minutes": 10,
        "shadow_max_hold_minutes": 120,
        "shadow_reference_notional_usdt": 20,
        "shadow_round_trip_cost_pct": 0.12,
    }

    result = update_shadow_trades([v3, v33], config)
    summary = shadow_summary(config=config)

    assert result["opened"] == 2
    assert {row["strategy_version"] for row in summary["trades"]} == {"v3.2", "v3.3-candidate"}
    assert {row["strategy_role"] for row in summary["trades"]} == {"active", "challenger"}
    assert len({row["opportunity_id"] for row in summary["trades"]}) == 1
    assert summary["active_release"]["strategy_version"] == "v3.2"
    assert summary["challenger_release"]["strategy_version"] == "v4.3.2"


def test_v4_decision_exploration_and_control_are_separate(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    base = {
        **_candidate(100),
        "entry_type": "v3_breakout",
        "signal": {"signal": "LONG", "last_price": 100, "stop": 95, "take_profit": 105},
        "strategy_role": "challenger",
        "opportunity_v4": {"shadow_eligible": True, "score": 80, "feature_schema_version": "v4.0"},
    }
    rows = [
        {**base, "strategy_family": "extreme_v4_roll", "strategy_version": "v4.0-candidate", "evidence_type": "decision"},
        {**base, "strategy_family": "extreme_v4_roll", "strategy_version": "v4.0-candidate", "evidence_type": "exploration"},
        {
            **base,
            "strategy_family": "extreme_v4_control",
            "strategy_version": "simple-breakout-v1",
            "evidence_type": "paired_control",
            "shadow_force_eligible": True,
        },
    ]
    config = {
        "shadow_trading_enabled": True,
        "opportunity_v4_strategy_version": "v4.0-candidate",
        "shadow_dedupe_minutes": 10,
        "shadow_max_hold_minutes": 120,
        "shadow_reference_notional_usdt": 20,
        "shadow_round_trip_cost_pct": 0.12,
    }

    result = update_shadow_trades(rows, config)
    summary = shadow_summary(config=config)

    assert result["opened"] == 3
    assert {row["evidence_type"] for row in summary["trades"]} == {"decision", "exploration", "paired_control"}
    assert len({row["opportunity_id"] for row in summary["trades"]}) == 1
    assert {row["signal_type"] for row in summary["trades"]} == {"breakout"}
    assert all("market_structure" in row["payload"] for row in summary["trades"])
    assert all("market_state" not in row["payload"] for row in summary["trades"])

    with connect() as conn:
        conn.execute(
            "UPDATE shadow_trades SET signal_type = 'v3_breakout', "
            "dedupe_key = REPLACE(dedupe_key, ':breakout:', ':v3_breakout:')"
        )
        conn.commit()

    duplicate_after_upgrade = update_shadow_trades(rows, config)
    assert duplicate_after_upgrade["opened"] == 0


def test_v4_shadow_summary_counts_one_decision_per_opportunity(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    config = {
        "shadow_trading_enabled": True,
        "opportunity_v4_strategy_version": "v5.4",
        "opportunity_v4_evidence_version": "v5.4",
        "shadow_dedupe_minutes": 0,
        "shadow_max_hold_minutes": 120,
        "shadow_reference_notional_usdt": 20,
        "shadow_round_trip_cost_pct": 0.12,
    }
    candidate = {
        **_candidate(100),
        "strategy_family": "extreme_v5_roll",
        "strategy_version": "v5.4",
        "strategy_role": "active",
        "evidence_type": "decision",
        "opportunity_v4": {"shadow_eligible": True, "score": 90},
    }
    assert update_shadow_trades([candidate], config)["opened"] == 1
    with connect() as conn:
        row = conn.execute("SELECT * FROM shadow_trades").fetchone()
        duplicate = dict(row)
        duplicate.pop("id", None)
        duplicate["dedupe_key"] = "duplicate-decision"
        duplicate["status"] = "CLOSED"
        duplicate["net_pnl"] = 9.0
        duplicate["estimated_cost"] = 0.1
        columns = ", ".join(duplicate)
        values = ", ".join("?" for _ in duplicate)
        conn.execute(f"INSERT INTO shadow_trades ({columns}) VALUES ({values})", tuple(duplicate.values()))
        conn.execute(
            "UPDATE shadow_trades SET status = 'CLOSED', net_pnl = -1, estimated_cost = 0.1 WHERE id = ?",
            (row["id"],),
        )
        conn.commit()

    summary = shadow_summary(config=config)
    assert summary["stats"]["closed"] == 1
    assert summary["stats"]["net_pnl"] == 9.0
    assert summary["active_release"]["independent_opportunity_only"] is True


def test_v55_shadow_summary_separates_research_only_from_executable_candidates(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    config = {
        "shadow_trading_enabled": True,
        "opportunity_v4_strategy_version": "v5.5",
        "opportunity_v4_evidence_version": "v5.5",
        "shadow_dedupe_minutes": 0,
        "shadow_max_hold_minutes": 120,
        "shadow_reference_notional_usdt": 20,
        "shadow_round_trip_cost_pct": 0.12,
    }
    executable = {
        **_candidate(100),
        "strategy_family": "extreme_v5_roll",
        "strategy_version": "v5.5",
        "strategy_role": "active",
        "evidence_type": "decision",
        "opportunity_v4": {"shadow_eligible": True, "score": 90, "admission_lane": "full_bet"},
    }
    research = {
        **_candidate(101),
        "symbol": "ETHUSDT",
        "strategy_family": "extreme_v5_roll",
        "strategy_version": "v5.5",
        "strategy_role": "active",
        "evidence_type": "decision",
        "opportunity_v4": {"shadow_eligible": True, "score": 60, "admission_lane": "shadow_only"},
    }
    assert update_shadow_trades([executable, research], config)["opened"] == 2
    with connect() as conn:
        rows = conn.execute("SELECT id, payload FROM shadow_trades ORDER BY id").fetchall()
        for row in rows:
            payload = row["payload"]
            if '"full_bet"' in payload:
                conn.execute("UPDATE shadow_trades SET status = 'CLOSED', net_pnl = 2, estimated_cost = 0.1 WHERE id = ?", (row["id"],))
            else:
                conn.execute("UPDATE shadow_trades SET status = 'CLOSED', net_pnl = -7, estimated_cost = 0.1 WHERE id = ?", (row["id"],))
        conn.commit()

    summary = shadow_summary(config=config)
    assert summary["stats"]["closed"] == 1
    assert summary["stats"]["net_pnl"] == 2.0
    assert summary["active_release"]["research_shadow_only"]["closed"] == 1
    assert summary["active_release"]["research_shadow_only"]["net_pnl"] == -7.0


def test_v43_shadow_counts_one_continuous_episode_until_price_resets(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    candidate = {
        **_candidate(100),
        "entry_type": "v3_breakout",
        "signal": {
            "signal": "LONG",
            "last_price": 100,
            "stop": 95,
            "take_profit": 110,
            "entry_phase": "RETEST",
        },
        "strategy_family": "extreme_v4_roll",
        "strategy_version": "v4.3",
        "strategy_role": "active",
        "evidence_type": "decision",
        "market_state": {"state": "broad_up"},
        "opportunity_v3": {
            "market_regime": "broad_up",
            "medium_trend_aligned": True,
            "medium_ready": True,
        },
        "opportunity_v4": {"shadow_eligible": True, "score": 80, "feature_schema_version": "v4.3"},
    }
    config = {
        "shadow_trading_enabled": True,
        "opportunity_v4_strategy_version": "v4.3",
        "shadow_dedupe_minutes": 10,
        "shadow_max_hold_minutes": 120,
        "shadow_reference_notional_usdt": 20,
        "shadow_round_trip_cost_pct": 0.12,
        "opportunity_v43_episode_dedupe_minutes": 30,
        "opportunity_v43_episode_reset_risk_multiple": 1.0,
    }

    first = update_shadow_trades([candidate], config)
    with connect() as conn:
        conn.execute(
            "UPDATE shadow_trades SET status = 'CLOSED', closed_at = opened_at, dedupe_key = 'closed-episode'"
        )
        conn.commit()
    duplicate = update_shadow_trades([candidate], config)

    reset = {**candidate, "signal": {**candidate["signal"], "last_price": 106}}
    restarted = update_shadow_trades([reset], config)

    assert first["opened"] == 1
    assert duplicate["opened"] == 0
    assert duplicate["episode_skipped"] == 1
    assert restarted["opened"] == 1
