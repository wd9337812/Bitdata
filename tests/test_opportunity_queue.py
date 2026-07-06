from __future__ import annotations

from app.opportunity_queue import enqueue_opportunity, read_opportunities, score_event


def test_enqueue_opportunity_keeps_highest_recent_event(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))

    first = enqueue_opportunity(
        symbol="fastusdt",
        event_type="kline_trigger",
        move_pct=0.5,
        quote_volume=300_000,
        interval="5m",
    )
    second = enqueue_opportunity(
        symbol="MOVEUSDT",
        event_type="kline_trigger",
        move_pct=-1.2,
        quote_volume=900_000,
        interval="5m",
    )

    events = read_opportunities(max_age_seconds=240, limit=10)
    assert first is not None
    assert second is not None
    assert events[0]["symbol"] == "MOVEUSDT"
    assert events[0]["direction_hint"] == "SHORT"
    assert events[1]["symbol"] == "FASTUSDT"


def test_low_score_opportunity_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))

    event = enqueue_opportunity(
        symbol="QUIETUSDT",
        event_type="kline_trigger",
        move_pct=0.01,
        quote_volume=1_000,
        min_score=50,
    )

    assert event is None
    assert read_opportunities() == []


def test_score_event_combines_move_and_volume():
    assert score_event("kline_trigger", 2.0, 1_000_000) > score_event("kline_trigger", 0.2, 100_000)
