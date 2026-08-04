import pandas as pd

from scripts.audit_s0_funding_crowding_reversal import (
    Profile,
    simulate,
    signals_for_profile,
)


def _panel() -> pd.DataFrame:
    rows = []
    for hour in range(200):
        rows.append(
            {
                "symbol": "TESTUSDT",
                "available_ms": 1_600_000_000_000 + hour * 3_600_000,
                "open": 100.0 + hour * 0.01,
                "high": 101.0 + hour * 0.01,
                "low": 99.0 + hour * 0.01,
                "close": 100.5 + hour * 0.01,
                "symbol_age_days": 300,
                "liquidity_24h": 100_000_000,
                "atr_24h": 0.5,
                "funding_rate_pct": 0.12,
                "funding_age_hours": 1.0,
            }
        )
    return pd.DataFrame(rows)


def test_signals_for_profile_picks_reversal_direction():
    panel = _panel()
    profile = Profile(
        "test",
        min_abs_funding_pct=0.10,
        hold_hours=24,
        stop_pct=0.10,
        take_r=2.0,
    )

    signals = signals_for_profile(panel, profile)

    assert len(signals) > 0
    assert (signals.direction == "SHORT").all()


def test_simulate_returns_trade_with_net_return():
    panel = _panel()
    profile = Profile(
        "test",
        min_abs_funding_pct=0.10,
        hold_hours=24,
        stop_pct=0.10,
        take_r=2.0,
    )
    signals = signals_for_profile(panel, profile)

    trades = simulate(signals, panel, profile, one_way_cost=0.0012)

    assert len(trades) > 0
    assert "net_return" in trades.columns
    assert (trades.direction == "SHORT").all()
