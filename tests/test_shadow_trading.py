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
