import pandas as pd

from scripts.audit_s0_altcoin_30d_live_candidate import (
    build_report,
    profit_factor,
    simulate_equity,
)


def test_profit_factor_and_hard_stop_simulation():
    assert profit_factor([10, -5, 5]) == 3
    stressed = simulate_equity([-15.6] * 8, risk_pct=30)
    assert stressed["hard_stop_hit"] is True
    selected = simulate_equity([44.4, -15.6, 10], risk_pct=15)
    assert selected["hard_stop_hit"] is False


def test_report_requires_positive_years_and_rejects_ruinous_risk():
    rows = pd.DataFrame(
        [
            {
                "entry_ms": 1704067200000,
                "exit_ms": 1704153600000,
                "symbol": "AUSDT",
                "direction": "LONG",
                "net_pct": 44.4,
                "cost_pct": 0.6,
            },
            {
                "entry_ms": 1735689600000,
                "exit_ms": 1735776000000,
                "symbol": "BUSDT",
                "direction": "SHORT",
                "net_pct": 20.0,
                "cost_pct": 0.6,
            },
        ]
    )
    report = build_report(rows)

    assert report["trades"] == 2
    assert report["decision"]["selected_risk_pct"] == 15.0
    assert report["decision"]["live_qualified"] is True
