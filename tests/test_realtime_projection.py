from __future__ import annotations

from app.account_projection import canonical_account_projection
from app.runner import background_scan_watchdog_reason
from app.smart_flow import enrich_smart_flow_candidates
from app.user_stream import _empty_state, _merge_event


def test_account_projection_prefers_live_private_stream(monkeypatch):
    snapshot = {
        **_empty_state(),
        "connected": True,
        "initialized": True,
        "updated_at": "2099-01-01T00:00:00+00:00",
        "account_updated_at": "2099-01-01T00:00:00+00:00",
        "account": {
            "totalWalletBalance": "20",
            "availableBalance": "18",
            "totalUnrealizedProfit": "1",
            "positions": [{"symbol": "TESTUSDT", "positionAmt": "2"}],
        },
    }
    monkeypatch.setattr("app.account_projection.read_user_stream_snapshot", lambda: snapshot)

    class Client:
        def account_live(self):
            raise AssertionError("REST should not be used while the private stream is live")

    result = canonical_account_projection(Client(), websocket_max_age_seconds=15)

    assert result["source"] == "private_websocket"
    assert result["account"]["equity"] == 21
    assert result["position_count"] == 1


def test_account_projection_falls_back_to_rest(monkeypatch):
    monkeypatch.setattr("app.account_projection.read_user_stream_snapshot", lambda: _empty_state())
    monkeypatch.setattr("app.account_projection.seed_user_account", lambda account: None)

    class Client:
        def account_live(self):
            return {
                "totalWalletBalance": "12",
                "availableBalance": "11",
                "totalUnrealizedProfit": "0",
                "positions": [],
            }

    result = canonical_account_projection(Client(), websocket_max_age_seconds=15)

    assert result["source"] == "binance_rest"
    assert result["account"]["equity"] == 12


def test_user_stream_prunes_terminal_orders_and_rejects_older_same_type_event():
    state = _empty_state()
    _merge_event(
        state,
        {"e": "ORDER_TRADE_UPDATE", "E": 200, "o": {"s": "TESTUSDT", "i": 1, "X": "NEW"}},
    )
    assert len(state["orders"]) == 1
    _merge_event(
        state,
        {"e": "ORDER_TRADE_UPDATE", "E": 100, "o": {"s": "TESTUSDT", "i": 1, "X": "FILLED"}},
    )
    assert len(state["orders"]) == 1
    _merge_event(
        state,
        {"e": "ORDER_TRADE_UPDATE", "E": 300, "o": {"s": "TESTUSDT", "i": 1, "X": "FILLED"}},
    )
    assert state["orders"] == {}


def test_background_watchdog_only_flags_dead_or_stalled_thread():
    state = {"in_flight": True, "started_monotonic": 10.0}
    assert background_scan_watchdog_reason(
        state, thread_alive=True, now_monotonic=80.0, timeout_seconds=90
    ) is None
    assert "stalled" in background_scan_watchdog_reason(
        state, thread_alive=True, now_monotonic=101.0, timeout_seconds=90
    )
    assert "exited" in background_scan_watchdog_reason(
        state, thread_alive=False, now_monotonic=20.0, timeout_seconds=90
    )


def test_smart_flow_is_bounded_soft_score(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    rows = [{"longShortRatio": "1.2"}, {"longShortRatio": "1.4"}]

    class Client:
        def top_trader_position_ratio(self, *args):
            return rows * 3

        def top_trader_account_ratio(self, *args):
            return rows * 3

        def taker_buy_sell_ratio(self, *args):
            return [{"buySellRatio": "1.2"}] * 6

        def open_interest_hist(self, *args):
            return [{"sumOpenInterestValue": "100"}, {"sumOpenInterestValue": "110"}] * 3

    candidates = [{"symbol": "TESTUSDT", "direction": "LONG", "score": 90, "ticker": {}, "derivatives": {}}]
    result = enrich_smart_flow_candidates(
        Client(),
        candidates,
        {
            "smart_flow_enabled": True,
            "smart_flow_live_soft_score_enabled": True,
            "smart_flow_symbol_limit": 1,
            "smart_flow_max_soft_points": 5,
            "smart_flow_min_confidence": 0.45,
            "smart_flow_cache_seconds": 300,
            "smart_flow_period": "5m",
            "smart_flow_history_points": 12,
        },
    )

    assert result[0]["smart_flow"]["available"] is True
    assert result[0]["smart_flow"]["confidence"] >= 0.45
    assert 0 < result[0]["smart_flow_score_delta"] <= 5
