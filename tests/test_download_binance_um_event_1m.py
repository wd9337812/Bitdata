import pandas as pd

from scripts.download_binance_um_event_1m import build_event_archives


def test_event_archives_cover_cross_day_hold() -> None:
    signal_time = int(pd.Timestamp("2026-07-01T18:00:00Z").timestamp() * 1000)
    signals = pd.DataFrame(
        {"symbol": ["BTCUSDT"], "available_ms": [signal_time]}
    )
    tasks = build_event_archives(signals, 24)
    assert [(task.symbol, task.date) for task in tasks] == [
        ("BTCUSDT", "2026-07-01"),
        ("BTCUSDT", "2026-07-02"),
    ]
    assert all("/1m/" in task.url for task in tasks)
