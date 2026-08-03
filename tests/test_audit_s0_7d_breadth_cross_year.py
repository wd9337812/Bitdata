from __future__ import annotations

import pandas as pd
import pytest

from scripts.audit_s0_7d_breadth_cross_year import (
    attach_market_breadth,
    frozen_breadth_filter,
)


def test_attach_and_filter_frozen_breadth_band() -> None:
    paths = pd.DataFrame(
        [
            {"signal_ms": 1, "symbol": "AAAUSDT", "direction": "LONG"},
            {"signal_ms": 2, "symbol": "BBBUSDT", "direction": "SHORT"},
            {"signal_ms": 3, "symbol": "CCCUSDT", "direction": "LONG"},
        ]
    )
    signals = pd.DataFrame(
        [
            {
                "available_ms": 1,
                "symbol": "AAAUSDT",
                "direction": "LONG",
                "market_breadth": 0.015,
            },
            {
                "available_ms": 2,
                "symbol": "BBBUSDT",
                "direction": "SHORT",
                "market_breadth": -0.075,
            },
            {
                "available_ms": 3,
                "symbol": "CCCUSDT",
                "direction": "LONG",
                "market_breadth": 0.10,
            },
        ]
    )

    result = frozen_breadth_filter(attach_market_breadth(paths, signals))

    assert result.symbol.tolist() == ["AAAUSDT", "BBBUSDT"]


def test_missing_market_breadth_fails_closed() -> None:
    paths = pd.DataFrame(
        [{"signal_ms": 1, "symbol": "AAAUSDT", "direction": "LONG"}]
    )
    signals = pd.DataFrame(
        columns=["available_ms", "symbol", "direction", "market_breadth"]
    )

    with pytest.raises(ValueError, match="no matching market breadth"):
        attach_market_breadth(paths, signals)
