from __future__ import annotations

from datetime import datetime, timezone

from app.binance_client import BinanceFuturesClient
from app import market_stream
from app.market_stream import overlay_stream_kline, write_snapshot, write_stream_intent


def test_stream_depth_overrides_rest(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    write_snapshot(
        {
            "connected": True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "symbols": ["BTCUSDT"],
            "tickers": {},
            "klines": {},
            "depths": {
                "BTCUSDT": {
                    "available": True,
                    "bids": [["100", "2"]],
                    "asks": [["101", "3"]],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            },
            "last_error": "",
        }
    )

    client = BinanceFuturesClient()
    depth = client.depth("BTCUSDT")

    assert depth["bids"][0] == ["100", "2"]
    assert depth["asks"][0] == ["101", "3"]


def test_overlay_stream_kline_replaces_current_bar(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    write_snapshot(
        {
            "connected": True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "symbols": ["BTCUSDT"],
            "tickers": {},
            "depths": {},
            "klines": {
                "BTCUSDT": {
                    "5m": {
                        "row": [1000, "1", "3", "0.5", "2.5", "9", 1999, "0", 0, "0", "0", "0"],
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                }
            },
            "last_error": "",
        }
    )

    rows = [[0, "1", "2", "0.5", "1.5", "1", 999, "0", 0, "0", "0", "0"], [1000, "1", "2", "0.5", "1.5", "1", 1999, "0", 0, "0", "0", "0"]]

    assert overlay_stream_kline(rows, "BTCUSDT", "5m")[-1][4] == "2.5"


def test_stream_ticker_overrides_symbol_subset(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    write_snapshot(
        {
            "connected": True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "symbols": ["BTCUSDT"],
            "tickers": {
                "BTCUSDT": {
                    "symbol": "BTCUSDT",
                    "lastPrice": "123",
                    "priceChangePercent": "4.5",
                    "quoteVolume": "999",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            },
            "klines": {},
            "depths": {},
            "last_error": "",
        }
    )

    class FakeClient(BinanceFuturesClient):
        def public_get(self, path, params=None):
            return [{"symbol": "BTCUSDT", "lastPrice": "100", "priceChangePercent": "1", "quoteVolume": "10"}]

    ticker = FakeClient().ticker_24h(["BTCUSDT"])[0]

    assert ticker["lastPrice"] == "123"
    assert ticker["quoteVolume"] == "999"


def test_stream_symbols_auto_discover_extends_manual_list(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(market_stream, "_discover_stream_symbols", lambda config, limit: ["ETHUSDT", "SOLUSDT", "AAVEUSDT"])

    symbols = market_stream._symbols_from_config(
        {
            "stage1_symbols": ["SOLUSDT", "LABUSDT"],
            "symbols": [],
            "stage2_symbols": [],
            "market_stream_max_symbols": 3,
            "market_stream_auto_discover": True,
        }
    )

    assert symbols == ["SOLUSDT", "LABUSDT", "ETHUSDT"]


def test_dynamic_stream_symbols_prioritize_positions_and_hot_intent(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(market_stream, "_discover_stream_symbols", lambda config, limit: ["ETHUSDT", "SOLUSDT", "AAVEUSDT"])
    write_stream_intent(
        position_symbols=["POSUSDT"],
        candidate_symbols=["HOTUSDT", "FASTUSDT"],
        hot_symbols=["MOVEUSDT"],
        live_credit_symbols=["CREDITUSDT"],
    )

    symbols = market_stream._symbols_from_config(
        {
            "stage1_symbols": ["SOLUSDT"],
            "symbols": [],
            "stage2_symbols": [],
            "market_stream_dynamic_enabled": True,
            "market_stream_max_symbols": 5,
            "market_stream_auto_discover": True,
            "stream_hot_symbols_limit": 3,
            "stream_include_positions": True,
            "stream_include_live_credit": True,
        }
    )

    assert symbols == ["POSUSDT", "SOLUSDT", "HOTUSDT", "FASTUSDT", "MOVEUSDT"]


def test_symbol_change_pct_counts_symmetric_difference():
    assert market_stream._symbol_change_pct(["AUSDT", "BUSDT"], ["AUSDT", "CUSDT"]) == 100.0


def test_kline_trigger_event_is_recorded_for_fast_move():
    captured = {}
    original = market_stream.enqueue_opportunity
    market_stream.enqueue_opportunity = lambda **kwargs: captured.update(kwargs) or {"symbol": kwargs["symbol"]}
    state = {"triggers": []}

    try:
        market_stream._append_trigger_event(
            state,
            "FASTUSDT",
            "5m",
            [1000, "10", "10.5", "9.9", "10.4", "100", 1999, "500000", 0, "0", "0", "0"],
            move_pct_threshold=0.3,
            quote_volume_threshold=250_000,
            max_events=5,
        )
    finally:
        market_stream.enqueue_opportunity = original

    assert state["triggers"][0]["symbol"] == "FASTUSDT"
    assert state["triggers"][0]["type"] == "kline_trigger"
    assert state["triggers"][0]["direction_hint"] == "LONG"
    assert captured["symbol"] == "FASTUSDT"
    assert captured["direction_hint"] == "LONG"


def test_full_orderbook_is_enabled_only_for_scalp_mode_candidates():
    symbols = ["POSUSDT", "HOTUSDT", "OTHERUSDT"]
    intent = {
        "active_mode": "yolo_scalp",
        "sources": {"positions": ["POSUSDT"], "candidates": ["HOTUSDT"], "hot": ["OTHERUSDT"]},
    }

    assert market_stream._full_orderbook_symbols(
        {"orderbook_full_stream_enabled": True, "orderbook_full_symbols_limit": 2}, symbols, intent
    ) == ["POSUSDT", "HOTUSDT"]
    assert market_stream._full_orderbook_symbols(
        {"orderbook_full_stream_enabled": True}, symbols, {**intent, "active_mode": "extreme_sprint"}
    ) == []


def test_full_orderbook_and_trade_flow_use_binance_stream_families():
    depth_url = market_stream._diff_depth_stream_url(["BTCUSDT", "ETHUSDT"])
    trade_url = market_stream._trade_stream_url(["BTCUSDT", "ETHUSDT"])

    assert "/public/stream?" in depth_url
    assert "btcusdt@depth@100ms" in depth_url
    assert "btcusdt@bookTicker" in depth_url
    assert "aggTrade" not in depth_url
    assert "/market/stream?" in trade_url
    assert "btcusdt@aggTrade" in trade_url
    assert "ethusdt@aggTrade" in trade_url


def test_book_ticker_and_trade_flow_metrics_capture_microstructure():
    book = market_stream._book_ticker_metrics({"b": "100", "B": "4", "a": "101", "A": "1"})
    buy = market_stream._trade_flow_metrics(
        {"s": "FLOWTESTUSDT", "T": 10_000, "p": "100", "q": "2", "m": False}, 5
    )
    mixed = market_stream._trade_flow_metrics(
        {"s": "FLOWTESTUSDT", "T": 11_000, "p": "100", "q": "1", "m": True}, 5
    )

    assert book["micro_price"] > book["mid_price"]
    assert buy["trade_flow_imbalance"] == 1
    assert mixed["trade_flow_notional"] == 300
    assert round(mixed["trade_flow_imbalance"], 4) == 0.3333
