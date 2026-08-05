from __future__ import annotations

import argparse
import json

from scripts.forward_event_monitor import (
    close_expired,
    detect_new_listings,
    evaluate_volume,
    select_30d_momentum,
    z_score,
)


def _args(**kwargs) -> argparse.Namespace:
    defaults = dict(
        symbols=["SOLUSDT"],
        funding_threshold_pct=0.05,
        funding_z=2.0,
        vol_z=3.0,
        btc_impulse_z=3.0,
        horizon_hours=24.0,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_z_score() -> None:
    values = [1.0 + index * 0.001 for index in range(100)]
    assert abs(z_score(values, 1.05)) < 1.0
    assert z_score(values, 3.0) > 3.0


def test_evaluate_volume_detects_shock(monkeypatch) -> None:
    klines = []
    for index in range(200):
        klines.append(
            [
                0, 0, 0, 0,  # open_time, open, high, low
                100.0 + (index % 5),  # close
                1000.0 + (index % 7) * 10.0,  # volume
                0, 0, 0, 0, 0, 0,
            ]
        )
    klines[-1][1] = 100.0
    klines[-1][4] = 101.0  # close > open
    klines[-1][5] = 50_000.0  # volume spike

    monkeypatch.setattr(
        "scripts.forward_event_monitor.fetch_klines",
        lambda symbol, interval, limit: klines,
    )
    event = evaluate_volume({}, "SOLUSDT", _args())
    assert event is not None
    assert event["type"] == "volume_breakout"
    assert event["direction"] == 1


def test_close_expired_writes_record(monkeypatch, tmp_path) -> None:
    records = tmp_path / "records.jsonl"
    state = {
        "open_events": [
            {
                "type": "volume_breakout",
                "symbol": "SOLUSDT",
                "direction": 1,
                "ts": 1_000_000_000_000,
                "entry_price": 100.0,
            }
        ]
    }
    monkeypatch.setattr(
        "scripts.forward_event_monitor.fetch_price",
        lambda symbol: 105.0,
    )
    monkeypatch.setattr(
        "scripts.forward_event_monitor.fetch_klines",
        lambda symbol, interval, limit: [
            [0, 0, 106.0, 99.0, 0, 0, 0, 0, 0, 0, 0, 0]
        ],
    )
    closed = close_expired(state, _args(horizon_hours=0.0), records)
    assert len(closed) == 1
    assert state["open_events"] == []
    line = json.loads(records.read_text(encoding="utf-8").strip())
    assert line["raw_return_pct"] == 5.0
    assert line["mfe_pct"] == 6.0


def test_select_30d_momentum_confirmations(monkeypatch) -> None:
    tickers = [{"symbol": "BTCUSDT", "quoteVolume": "1000000000", "lastPrice": "64000"}]
    strong = "STRONGUSDT"
    ratios = {"BTCUSDT": 1.05}
    for index in range(40):
        symbol = f"UP{index:02d}USDT"
        tickers.append({"symbol": symbol, "quoteVolume": "50000000", "lastPrice": "10"})
        ratios[symbol] = 1.03
    for index in range(20):
        symbol = f"DOWN{index:02d}USDT"
        tickers.append({"symbol": symbol, "quoteVolume": "50000000", "lastPrice": "10"})
        ratios[symbol] = 0.98
    for value in (1.08, 1.09, 1.10):
        symbol = f"BIG{int(value * 100)}USDT"
        tickers.append({"symbol": symbol, "quoteVolume": "50000000", "lastPrice": "10"})
        ratios[symbol] = value
    ratios[strong] = 1.15
    tickers.append({"symbol": strong, "quoteVolume": "50000000", "lastPrice": "30"})

    def fake_get_json(url, params=None):
        assert "ticker/24hr" in url
        return tickers

    def fake_klines(symbol, interval, limit):
        if interval == "1d":
            close = {"BTCUSDT": 105.0, strong: 110.0}
            ratio = ratios[symbol]
            close_value = close.get(symbol, 100.0 * ratio)
            return [[0, 0, 0, 0, close_value / ratio, 0, 0, 0, 0, 0, 0, 0]] * 31 + [
                [0, 0, 0, 0, close_value, 0, 0, 0, 0, 0, 0, 0]
            ]
        # 1h
        if symbol == strong:
            return [[0, 0, 0, 0, 30.0, 0, 0, 0, 0, 0, 0, 0]] * 24 + [
                [0, 0, 0, 0, 30.3, 0, 0, 0, 0, 0, 0, 0]
            ]
        return [[0, 0, 0, 0, 100.0, 0, 0, 0, 0, 0, 0, 0]] * 25

    monkeypatch.setattr("scripts.forward_event_monitor._get_json", fake_get_json)
    monkeypatch.setattr("scripts.forward_event_monitor.fetch_klines", fake_klines)
    result = select_30d_momentum(_args())
    assert result is not None
    assert result["symbol"] == strong
    assert result["confirmed"] is True
    assert result["acceleration"] is True
    assert result["breadth_confirmation"] is True


def test_detect_new_listings(monkeypatch) -> None:
    def fake_get_json(url, params=None):
        return {
            "symbols": [
                {"symbol": "BTCUSDT"},
                {"symbol": "NEWALTUSDT"},
                {"symbol": "OLDALTUSDT"},
            ]
        }

    monkeypatch.setattr("scripts.forward_event_monitor._get_json", fake_get_json)
    monkeypatch.setattr(
        "scripts.forward_event_monitor.fetch_price", lambda symbol: 1.23
    )
    state: dict = {}
    events = detect_new_listings(state)
    assert events == []  # first run only initializes
    assert state["known_symbols"] == ["BTCUSDT", "NEWALTUSDT", "OLDALTUSDT"]
    events2 = detect_new_listings(state)
    assert len(events2) == 0
    state["known_symbols"].remove("NEWALTUSDT")
    state["known_symbols"] = ["BTCUSDT", "OLDALTUSDT"]
    events3 = detect_new_listings(state)
    assert len(events3) == 1
    assert events3[0]["type"] == "new_listing"
    assert events3[0]["symbol"] == "NEWALTUSDT"
    assert events3[0]["direction"] is None


def test_close_expired_absolute_for_direction_none(monkeypatch, tmp_path) -> None:
    records = tmp_path / "records.jsonl"
    state = {
        "open_events": [
            {
                "type": "new_listing",
                "symbol": "NEWALTUSDT",
                "direction": None,
                "ts": 1_000_000_000_000,
                "entry_price": 1.0,
            }
        ]
    }
    monkeypatch.setattr(
        "scripts.forward_event_monitor.fetch_price", lambda symbol: 0.8
    )
    monkeypatch.setattr(
        "scripts.forward_event_monitor.fetch_klines",
        lambda symbol, interval, limit: [
            [0, 0, 1.3, 0.7, 0, 0, 0, 0, 0, 0, 0, 0]
        ],
    )
    closed = close_expired(state, _args(horizon_hours=0.0), records)
    assert closed[0]["raw_return_pct"] == 20.0
    assert closed[0]["mfe_pct"] == round(42.8571, 4)


def test_close_expired_new_listing_first_hour_rule(monkeypatch, tmp_path) -> None:
    records = tmp_path / "records2.jsonl"
    ts = 1_000_000_000_000
    state = {
        "open_events": [
            {
                "type": "new_listing",
                "symbol": "NEWALTUSDT",
                "direction": None,
                "ts": ts,
                "entry_price": 1.0,
                "horizon_hours": 0.0,
            }
        ]
    }
    monkeypatch.setattr(
        "scripts.forward_event_monitor.fetch_price", lambda symbol: 1.0
    )
    monkeypatch.setattr(
        "scripts.forward_event_monitor.fetch_klines",
        lambda symbol, interval, limit: [
            [ts, 1.0, 1.12, 0.98, 1.10, 0, 0, 0, 0, 0, 0, 0],
            [ts + 3_600_000, 0, 1.15, 0.80, 0.90, 0, 0, 0, 0, 0, 0, 0],
        ],
    )
    closed = close_expired(state, _args(horizon_hours=0.0), records)
    assert closed[0]["first_hour_return_pct"] == 10.0
    assert closed[0]["first_hour_rule_traded"] is True
    assert closed[0]["first_hour_rule_pnl_pct"] == -15.0
