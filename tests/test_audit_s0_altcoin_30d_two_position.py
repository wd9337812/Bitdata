import pandas as pd

from scripts.audit_s0_altcoin_30d_two_position import (
    gate_max_positions,
    simulate_equity,
)


def test_gate_allows_up_to_two_concurrent_positions():
    rows = pd.DataFrame(
        [
            {
                "entry_ms": 1000,
                "exit_ms": 5000,
                "symbol": "AUSDT",
                "direction": "LONG",
                "strength": 0.9,
                "net_pct": 20.0,
            },
            {
                "entry_ms": 2000,
                "exit_ms": 6000,
                "symbol": "BUSDT",
                "direction": "SHORT",
                "strength": 0.8,
                "net_pct": 15.0,
            },
            {
                "entry_ms": 3000,
                "exit_ms": 7000,
                "symbol": "CUSDT",
                "direction": "LONG",
                "strength": 0.7,
                "net_pct": -10.0,
            },
        ]
    )
    one = gate_max_positions(rows, max_positions=1)
    two = gate_max_positions(rows, max_positions=2)

    assert len(one) == 1
    assert len(two) == 2


def test_simulate_equity_hits_hard_stop_on_repeated_losses():
    result = simulate_equity([-15.6] * 8, risk_pct=20.0)

    assert result["hard_stop_hit"] is True
