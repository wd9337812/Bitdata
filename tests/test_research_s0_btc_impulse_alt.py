from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.research_s0_btc_impulse_alt import (
    detect_events,
    rolling_daily_volume,
    select_alt,
)


def _frame(prices: list[float], start_ms: int = 0) -> pd.DataFrame:
    rows = []
    for index, price in enumerate(prices):
        rows.append(
            {
                "open_time": start_ms + index * 60_000,
                "open": float(price),
                "high": float(price) * 1.001,
                "low": float(price) * 0.999,
                "close": float(price),
                "volume": 100.0,
                "quote_volume": 100.0 * float(price),
            }
        )
    return pd.DataFrame(rows)


def test_detect_events_fires_after_bar_close() -> None:
    prices = [100.0 + 0.1 * np.sin(index / 7.0) for index in range(700)]
    # Strong +6% jump inside the 5m bar that starts at minute 500.
    for index in range(500, 505):
        prices[index] = prices[index - 5] * 1.06
    btc = _frame(prices)
    events = detect_events(
        btc,
        event_bar_minutes=5,
        lookback_minutes=30,
        z_threshold=3.0,
        std_window_bars=120,
        allow_short=False,
    )
    assert len(events) >= 1
    event = events[0]
    assert event.direction == 1
    # The event fires at the close of the 5m bar (minute 505), never before it.
    assert event.time_ms == 505 * 60_000
    assert event.z > 3.0


def test_rolling_daily_volume_uses_only_past() -> None:
    prices = [100.0] * 60_000
    frame = _frame(prices)
    frame.loc[40_000:, "quote_volume"] = 10_000_000.0
    day_idx, rolling = rolling_daily_volume(frame)
    assert len(day_idx) > 0
    assert np.all(np.isfinite(rolling[rolling > 0]))
    # The last value is a 21-day mean ending at the final day, no future shift.
    assert float(rolling[-1]) > 1_000_000.0


def test_select_alt_ranks_by_point_in_time_volume() -> None:
    cache = {}
    # daily_target_usdt is the intended 21d average daily quote volume.
    for symbol, price, daily_target_usdt, onboard in (
        ("ETHUSDT", 100.0, 1_000_000.0, 0),
        ("SOLUSDT", 50.0, 5_000_000.0, 0),
        ("NEWUSDT", 10.0, 99_000_000.0, 999_999_999_999),
    ):
        frame = _frame([price] * 12_000)
        frame["quote_volume"] = daily_target_usdt / 1440.0
        day_idx, rv = rolling_daily_volume(frame)
        cache[symbol] = (frame, day_idx, rv, onboard)
    # Event happens after day 7 (rolling volume is defined) and before NEWUSDT's onboard date.
    chosen = select_alt(cache, event_time_ms=11_000 * 60_000, min_volume=0.0)
    assert chosen == "SOLUSDT"
    # With a high minimum, none qualifies.
    assert (
        select_alt(cache, event_time_ms=11_000 * 60_000, min_volume=10_000_000.0)
        is None
    )
