from __future__ import annotations

import argparse
import json

from scripts.forward_event_monitor import (
    close_expired,
    evaluate_volume,
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
