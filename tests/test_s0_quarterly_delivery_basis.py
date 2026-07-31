import pandas as pd
import pytest

from scripts import benchmark_s0_quarterly_delivery_basis as subject


def test_current_contract_uses_nearest_unexpired_delivery() -> None:
    available = {"BTCUSD_220325", "BTCUSD_220624"}
    close_time = int(pd.Timestamp("2022-03-24 23:59:59.999", tz="UTC").timestamp() * 1000)

    assert subject.current_contract("BTC", close_time, available) == "BTCUSD_220325"


def test_select_uses_top_and_bottom_thirds() -> None:
    panel = pd.DataFrame(
        {
            "close_time": [1] * 9,
            "symbol": [f"S{i}" for i in range(9)],
            "basis": list(range(9)),
            "distance": [abs(i - 4) for i in range(9)],
            "median_basis": [4] * 9,
        }
    )
    result = subject.select(panel, "high_low")

    assert set(result.loc[result.side.eq("long"), "symbol"]) == {"S6", "S7", "S8"}
    assert set(result.loc[result.side.eq("short"), "symbol"]) == {"S0", "S1", "S2"}


def market_frame(entry_time: int, falling: bool = False) -> pd.DataFrame:
    times = [entry_time - subject.DAY_MS + i * subject.FIVE_MINUTES_MS for i in range(578)]
    rows = []
    for index, time in enumerate(times):
        price = 100.0
        low = 60.0 if falling and time == entry_time + subject.FIVE_MINUTES_MS else 99.0
        rows.append(
            {
                "open_time": time,
                "open": price,
                "high": 101.0,
                "low": low,
                "close": price,
                "quote_volume": 100_000.0,
            }
        )
    return pd.DataFrame(rows)


def test_execute_includes_funding_only_before_exit() -> None:
    entry_time = 1_700_000_000_000
    signal_close = entry_time - 1 - subject.FIVE_MINUTES_MS
    funding = pd.DataFrame(
        {
            "calc_time": [entry_time + 8 * 60 * 60 * 1000, entry_time + 30 * 60 * 60 * 1000],
            "last_funding_rate": [0.001, 0.5],
        }
    )
    result = subject.execute(market_frame(entry_time), funding, signal_close, "long", None)

    assert result is not None
    assert result["funding_return"] == pytest.approx(-0.001)


def test_execute_stop_uses_adverse_gap_and_stops_funding() -> None:
    entry_time = 1_700_000_000_000
    signal_close = entry_time - 1 - subject.FIVE_MINUTES_MS
    market = market_frame(entry_time, falling=True)
    hit_time = entry_time + subject.FIVE_MINUTES_MS
    market.loc[market.open_time.eq(hit_time), "open"] = 65.0
    funding = pd.DataFrame(
        {"calc_time": [entry_time + 8 * 60 * 60 * 1000], "last_funding_rate": [0.01]}
    )
    result = subject.execute(market, funding, signal_close, "long", 0.30)

    assert result is not None
    assert result["exit_reason"] == "stop"
    assert result["price_return"] == pytest.approx(-0.35)
    assert result["funding_return"] == 0.0
