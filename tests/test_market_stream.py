from __future__ import annotations

from datetime import datetime, timezone

from app.binance_client import BinanceFuturesClient
from app import market_stream
from app.market_stream import overlay_stream_kline, write_snapshot


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


def test_stream_symbols_auto_discover_extends_manual_list(monkeypatch):
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
