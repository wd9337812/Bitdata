import pandas as pd

from scripts.benchmark_s0_point_in_time_funding import (
    FundingCandidate,
    funding_signals,
)


def test_crowded_reversal_uses_opposite_funding_direction() -> None:
    panel = pd.DataFrame(
        {
            "symbol": ["AUSDT", "BUSDT"],
            "available_ms": [1, 1],
            "symbol_age_days": [60, 60],
            "liquidity_24h": [30_000_000, 30_000_000],
            "funding_age_hours": [1, 1],
            "funding_rate_pct": [0.06, -0.06],
            "ret_6h": [0.02, -0.02],
        }
    )
    candidate = FundingCandidate(
        "test",
        "crowded_reversal",
        0.05,
    )
    signals = funding_signals(panel, candidate, 30)
    assert len(signals) == 1
    assert signals.iloc[0].direction in {"LONG", "SHORT"}
    if signals.iloc[0].symbol == "AUSDT":
        assert signals.iloc[0].direction == "SHORT"
    else:
        assert signals.iloc[0].direction == "LONG"


def test_squeeze_follows_price_against_funding() -> None:
    panel = pd.DataFrame(
        {
            "symbol": ["AUSDT"],
            "available_ms": [1],
            "symbol_age_days": [60],
            "liquidity_24h": [30_000_000],
            "funding_age_hours": [1],
            "funding_rate_pct": [-0.06],
            "ret_6h": [0.02],
        }
    )
    candidate = FundingCandidate("test", "squeeze", 0.05)
    signals = funding_signals(panel, candidate, 30)
    assert signals.iloc[0].direction == "LONG"


def test_funding_continuation_follows_funding_sign() -> None:
    panel = pd.DataFrame(
        {
            "symbol": ["AUSDT"],
            "available_ms": [1],
            "symbol_age_days": [60],
            "liquidity_24h": [30_000_000],
            "funding_age_hours": [1],
            "funding_rate_pct": [0.06],
            "ret_6h": [0.02],
        }
    )
    candidate = FundingCandidate("test", "continuation", 0.05)
    signals = funding_signals(panel, candidate, 30)
    assert signals.iloc[0].direction == "LONG"
