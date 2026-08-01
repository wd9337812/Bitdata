from types import SimpleNamespace

import pandas as pd

from scripts.benchmark_s0_momentum_execution_confirmation import (
    _confirmed_entry,
    cohort,
    qualifies,
)


def _minute_path(start_ms: int) -> pd.DataFrame:
    rows = []
    first_ms = start_ms - 21 * 5 * 60_000
    for minute in range(24 * 5):
        bucket = minute // 5
        base = 100.0 + bucket * 0.1
        open_price = base + (minute % 5) * 0.01
        close = open_price + 0.01
        rows.append(
            {
                "open_time": first_ms + minute * 60_000,
                "open": open_price,
                "high": close + 0.01,
                "low": open_price - 0.01,
                "close": close,
                "quote_volume": 1000.0,
                "taker_buy_quote_volume": 600.0,
            }
        )
    return pd.DataFrame(rows).set_index("open_time")


def test_cohort_is_stable() -> None:
    assert cohort("BTCUSDT") == cohort("BTCUSDT")
    assert cohort("BTCUSDT") in {"research", "validation", "blind"}


def test_confirmation_enters_on_next_five_minute_open() -> None:
    start_ms = 1_800_000_000_000
    signal = SimpleNamespace(available_ms=start_ms, direction="LONG")
    path = _minute_path(start_ms)
    result = _confirmed_entry(signal, path)
    assert result is not None
    entry_ms, entry = result
    assert entry_ms == start_ms + 5 * 60_000
    assert entry == path.loc[entry_ms, "open"]


def _metrics(trades: int = 20, pf: float = 1.2, net: float = 1.0) -> dict:
    return {"trades": trades, "profit_factor": pf, "net_pct_points": net}


def _reports() -> dict:
    base = {
        "windows": {
            name: _metrics()
            for name in ("validation_apr_may", "test_june", "final_july")
        },
        "cohorts": {"blind": _metrics()},
        "without_top_3_symbols": _metrics(),
        "overall": _metrics(),
    }
    return {"base": base, "stress": {"overall": _metrics()}}


def test_qualification_requires_each_frozen_gate() -> None:
    reports = _reports()
    assert qualifies(reports)
    reports["base"]["windows"]["test_june"] = _metrics(net=-1.0)
    assert not qualifies(reports)


def test_qualification_requires_positive_stress_result() -> None:
    reports = _reports()
    reports["stress"]["overall"] = _metrics(net=-0.1)
    assert not qualifies(reports)
