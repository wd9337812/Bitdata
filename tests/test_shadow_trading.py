from app.shadow_trading import shadow_summary, update_shadow_trades


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
    assert summary["challenger_release"]["strategy_version"] == "v3.3-candidate"
