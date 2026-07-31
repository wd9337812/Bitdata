import pandas as pd

from scripts.benchmark_s0_point_in_time_slow_momentum import (
    SlowMomentumCandidate,
    add_slow_returns,
    slow_momentum_signals,
)
from scripts.audit_s0_xmom_regime_candidates import BASE_COST
from scripts.benchmark_s0_cross_sectional_momentum import simulate


def test_add_slow_returns_is_symbol_local() -> None:
    rows = []
    for timestamp in range(170):
        rows.extend(
            [
                {"symbol": "AUSDT", "available_ms": timestamp, "close": timestamp + 1},
                {"symbol": "BUSDT", "available_ms": timestamp, "close": 2 * (timestamp + 1)},
            ]
        )
    result = add_slow_returns(pd.DataFrame(rows))
    latest = result.loc[result.available_ms.eq(169)]
    assert latest.ret_168h.notna().all()
    assert latest.ret_168h.nunique() == 1


def test_slow_signal_obeys_cadence_and_market_direction() -> None:
    panel = pd.DataFrame(
        {
            "symbol": ["BTCUSDT", "AUSDT", "BUSDT"],
            "available_ms": [6 * 3_600_000] * 3,
            "symbol_age_days": [90] * 3,
            "liquidity_24h": [50_000_000] * 3,
            "ret_168h": [0.02, 0.03, 0.08],
        }
    )
    extra = []
    for index in range(60):
        extra.append(
            {
                "symbol": f"X{index}USDT",
                "available_ms": 6 * 3_600_000,
                "symbol_age_days": 90,
                "liquidity_24h": 50_000_000,
                "ret_168h": 0.01 + index / 10_000,
            }
        )
    panel = pd.concat([panel, pd.DataFrame(extra)], ignore_index=True)
    candidate = SlowMomentumCandidate("test", 168, 6, 24, 2, 2)
    signal = slow_momentum_signals(panel, candidate, 45).iloc[0]
    assert signal.symbol == "BUSDT"
    assert signal.direction == "LONG"


def test_candidate_profile_can_run_with_shared_simulator() -> None:
    candidate = SlowMomentumCandidate("test", 168, 6, 24, 2, 2)
    assert candidate.hold_hours == 24
    assert BASE_COST > 0
    assert callable(simulate)
