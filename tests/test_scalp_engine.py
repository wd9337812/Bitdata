from __future__ import annotations

from datetime import datetime, timezone

from app.market_stream import write_snapshot
from app.scalp_engine import build_scalp_signal


def _write_1m(symbol: str, row: list[object]) -> None:
    write_snapshot(
        {
            "connected": True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "symbols": [symbol],
            "tickers": {},
            "depths": {},
            "klines": {
                symbol: {
                    "1m": {
                        "row": row,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                }
            },
            "last_error": "",
        }
    )


def test_orderbook_scalp_passes_on_fresh_event_depth_and_net_profit(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    _write_1m("FASTUSDT", [1000, "10", "10.2", "9.9", "10.1", "1000", 1999, "120000", 0, "0", "0", "0"])

    result = build_scalp_signal(
        symbol="FASTUSDT",
        direction="LONG",
        bars=[[i, 10, 10.2, 9.9, 10.1, 1000, i + 1, 100000] for i in range(90)],
        ticker={"lastPrice": "10.1", "priceChangePercent": "3", "quoteVolume": "50000000"},
        depth={
            "available": True,
            "spread_pct": 0.02,
            "depth_notional": 5000,
            "bids": [["10.00", "420"], ["9.99", "140"]],
            "asks": [["10.01", "120"], ["10.02", "80"]],
        },
        event={
            "symbol": "FASTUSDT",
            "direction_hint": "LONG",
            "move_pct": 0.4,
            "quote_volume": 300000,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        base_signal={"last_price": 10.1, "atr": 0.08, "expected_profit_pct": 0.24},
        recent={"profit_factor": 1.0},
        config={},
    )

    assert result["passed"] is True
    assert result["entry_type"] == "orderbook_impact"
    assert result["signal"]["entry_type_label"] == "盘口冲击"
    assert result["signal"]["protection_profile"]["max_hold_seconds"] == 120


def test_orderbook_scalp_blocks_wide_spread(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    write_snapshot(
        {
            "connected": True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "symbols": ["WIDEUSDT"],
            "tickers": {},
            "depths": {},
            "klines": {},
            "last_error": "",
        }
    )

    result = build_scalp_signal(
        symbol="WIDEUSDT",
        direction="LONG",
        bars=[[i, 10, 10.2, 9.9, 10.1, 1000, i + 1, 100000] for i in range(90)],
        ticker={"lastPrice": "10.1", "priceChangePercent": "3", "quoteVolume": "50000000"},
        depth={
            "available": True,
            "spread_pct": 0.3,
            "depth_notional": 5000,
            "bids": [["10.00", "420"]],
            "asks": [["10.03", "100"]],
        },
        event={
            "symbol": "WIDEUSDT",
            "direction_hint": "LONG",
            "move_pct": 0.4,
            "quote_volume": 300000,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        base_signal={"last_price": 10.1, "atr": 0.08, "expected_profit_pct": 0.1},
        recent={"profit_factor": 1.0},
        config={},
    )

    assert result["passed"] is False
    assert any("点差过大" in item for item in result["blockers"])


def test_orderbook_scalp_probe_can_use_lower_depth(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    _write_1m("THINUSDT", [1000, "10", "10.1", "9.9", "10.02", "1000", 1999, "40000", 0, "0", "0", "0"])

    result = build_scalp_signal(
        symbol="THINUSDT",
        direction="LONG",
        bars=[[i, 10, 10.2, 9.9, 10.02, 1000, i + 1, 100000] for i in range(90)],
        ticker={"lastPrice": "10.02", "priceChangePercent": "1", "quoteVolume": "5000000"},
        depth={
            "available": True,
            "spread_pct": 0.02,
            "depth_notional": 420,
            "bids": [["10.00", "32"], ["9.99", "8"]],
            "asks": [["10.01", "2"], ["10.02", "1"]],
        },
        event={
            "symbol": "THINUSDT",
            "direction_hint": "LONG",
            "move_pct": 0.1,
            "quote_volume": 40000,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        base_signal={"last_price": 10.02, "atr": 0.08, "expected_profit_pct": 0.2},
        recent={"profit_factor": 1.0},
        config={},
    )

    assert result["passed"] is True
    assert result["entry_type"] == "imbalance_probe"
    assert result["required_depth_notional"] == 300


def test_orderbook_scalp_strong_imbalance_can_probe_direction_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    _write_1m("FLIPUSDT", [1000, "10", "10.2", "9.9", "10.05", "1000", 1999, "80000", 0, "0", "0", "0"])

    result = build_scalp_signal(
        symbol="FLIPUSDT",
        direction="LONG",
        bars=[[i, 10, 10.2, 9.9, 10.05, 1000, i + 1, 100000] for i in range(90)],
        ticker={"lastPrice": "10.05", "priceChangePercent": "-1", "quoteVolume": "5000000"},
        depth={
            "available": True,
            "spread_pct": 0.02,
            "depth_notional": 800,
            "bids": [["10.00", "70"], ["9.99", "10"]],
            "asks": [["10.01", "4"], ["10.02", "2"]],
        },
        event={
            "symbol": "FLIPUSDT",
            "direction_hint": "SHORT",
            "move_pct": 0.1,
            "quote_volume": 80000,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        base_signal={"last_price": 10.05, "atr": 0.08, "expected_profit_pct": 0.2},
        recent={"profit_factor": 1.0},
        config={},
    )

    assert result["passed"] is True
    assert result["entry_type"] == "imbalance_probe"
    assert result["direction_probe"] is True


def test_orderbook_scalp_reads_actual_stream_kline_row_without_queue_event(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    _write_1m("ROWUSDT", [1000, "10", "10.2", "9.9", "10.1", "1000", 1999, "80000", 0, "0", "0", "0"])

    result = build_scalp_signal(
        symbol="ROWUSDT",
        direction="LONG",
        bars=[[i, 10, 10.2, 9.9, 10.1, 1000, i + 1, 100000] for i in range(90)],
        ticker={"lastPrice": "10.1", "priceChangePercent": "1", "quoteVolume": "5000000"},
        depth={
            "available": True,
            "spread_pct": 0.02,
            "depth_notional": 5000,
            "bids": [["10.00", "420"]],
            "asks": [["10.01", "120"]],
        },
        event=None,
        base_signal={"last_price": 10.1, "atr": 0.08, "expected_profit_pct": 0.2},
        recent={"profit_factor": 1.0},
        config={},
    )

    assert result["one_minute_quote_volume"] == 80000
    assert result["entry_type"] == "volume_scalp"
    assert result["passed"] is True


def test_orderbook_scalp_blends_active_trade_flow_with_l2_imbalance(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    _write_1m("FLOWUSDT", [1000, "10", "10.2", "9.9", "10.1", "1000", 1999, "80000", 0, "0", "0", "0"])

    result = build_scalp_signal(
        symbol="FLOWUSDT",
        direction="LONG",
        bars=[[i, 10, 10.2, 9.9, 10.1, 1000, i + 1, 100000] for i in range(90)],
        ticker={"lastPrice": "10.1", "priceChangePercent": "1", "quoteVolume": "5000000"},
        depth={
            "available": True,
            "spread_pct": 0.02,
            "depth_notional": 5000,
            "bids": [["10.00", "200"]],
            "asks": [["10.01", "190"]],
            "trade_flow_notional": 5000,
            "trade_flow_imbalance": 0.8,
            "microprice_edge_bps": 0.4,
        },
        event={"direction_hint": "LONG", "move_pct": 0.2, "quote_volume": 100000, "updated_at": datetime.now(timezone.utc).isoformat()},
        base_signal={"last_price": 10.1, "atr": 0.08, "expected_profit_pct": 0.2},
        recent={"profit_factor": 1.0},
        config={"yolo_scalp_trade_flow_weight": 0.5},
    )

    assert result["combined_imbalance"] > result["imbalance"]
    assert result["directed_trade_flow_imbalance"] == 0.8
    assert "主动成交方向一致" in result["reasons"]
