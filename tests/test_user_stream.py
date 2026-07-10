from __future__ import annotations

from app import user_stream


def test_user_stream_account_update_merges_balance_and_position(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    user_stream.seed_user_account(
        {
            "totalWalletBalance": "50",
            "availableBalance": "50",
            "assets": [{"asset": "USDT", "walletBalance": "50", "crossWalletBalance": "50"}],
            "positions": [],
        }
    )
    state = user_stream.read_user_stream_snapshot()

    user_stream._merge_event(
        state,
        {
            "e": "ACCOUNT_UPDATE",
            "a": {
                "B": [{"a": "USDT", "wb": "48.5", "cw": "30.5"}],
                "P": [{"s": "SOLUSDT", "ps": "BOTH", "pa": "2", "ep": "100", "bep": "100.1", "up": "1.25", "iw": "18"}],
            },
        },
    )
    user_stream._write_state(state)
    account = user_stream.account_from_user_stream(90)

    assert account is not None
    assert account["totalWalletBalance"] == "48.5"
    assert account["availableBalance"] == "50"
    assert account["availableBalanceSource"] == "rest_snapshot"
    assert account["totalUnrealizedProfit"] == "1.25"
    assert account["positions"][0]["symbol"] == "SOLUSDT"


def test_user_stream_tracks_order_events_without_persisting_listen_key(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    state = user_stream.read_user_stream_snapshot()
    state["listen_key"] = "must-not-be-written"
    user_stream._merge_event(
        state,
        {"e": "ORDER_TRADE_UPDATE", "o": {"s": "BTCUSDT", "i": 123, "X": "FILLED"}},
    )
    user_stream._write_state(state)
    persisted = (tmp_path / "user_stream.json").read_text(encoding="utf-8")
    snapshot = user_stream.read_user_stream_snapshot()

    assert "must-not-be-written" not in persisted
    assert snapshot["orders"]["BTCUSDT:123"]["X"] == "FILLED"
    assert snapshot["last_event_type"] == "ORDER_TRADE_UPDATE"
