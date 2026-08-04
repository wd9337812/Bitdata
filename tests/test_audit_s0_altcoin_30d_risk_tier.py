import pandas as pd

from scripts.audit_s0_altcoin_30d_risk_tier import (
    build_tier_report,
    simulate_tiered,
)


def test_tier_uses_base_risk_below_equity_line_and_upgrades_above():
    # A loss below 30U must be sized at 20%; the same loss above 30U at 22%.
    low = simulate_tiered([-15.0], start_equity=15.0)
    high = simulate_tiered([-15.0], start_equity=60.0)

    assert low["final_equity"] == round(15.0 * (1 - 0.20), 8)
    assert high["final_equity"] == round(60.0 * (1 - 0.22), 8)
    assert low["tier2_trades"] == 0
    assert high["tier2_trades"] == 1


def test_tier_report_promotes_only_when_no_yearly_hard_stop():
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
                "net_pct": 44.4,
                "cost_pct": 0.6,
            },
            {
                "entry_ms": 1767225600000,
                "exit_ms": 1767312000000,
                "symbol": "CUSDT",
                "direction": "LONG",
                "net_pct": 44.4,
                "cost_pct": 0.6,
            },
        ]
    )
    report = build_tier_report(rows)

    assert report["tiered"]["hard_stop_hit"] is False
    assert report["tiered"]["final_equity"] > report["flat20"]["final_equity"]
    assert report["promoted"] is True
