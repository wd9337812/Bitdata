from __future__ import annotations

from datetime import datetime, timezone

from app.market_stream import write_snapshot
from app.scalp_engine import build_scalp_signal


def test_orderbook_scalp_passes_on_fresh_event_depth_and_net_profit(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    write_snapshot(
        {
            "connected": True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "symbols": ["FASTUSDT"],
            "tickers": {},
            "depths": {},
            "klines": {
                "FASTUSDT": {
                    "1m": {
                        "row": [1000, "10", "10.2", "9.9", "10.1", "1000", 1999, "120000", 0, "0", "0", "0"],
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                }
            },
            "last_error": "",
        }
    )
    depth = {
        "available": True,
        "spread_pct": 0.02,
        "depth_notional": 5000,
        "bids": [["10.00", "420"], ["9.99", "140"]],
        "asks": [["10.01", "120"], ["10.02", "80"]],
    }

    result = build_scalp_signal(
        symbol="FASTUSDT",
        direction="LONG",
        bars=[[i, 10, 10.2, 9.9, 10.1, 1000, i + 1, 100000] for i in range(90)],
        ticker={"lastPrice": "10.1", "priceChangePercent": "3", "quoteVolume": "50000000"},
        depth=depth,
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
