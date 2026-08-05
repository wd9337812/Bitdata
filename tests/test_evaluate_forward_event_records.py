from __future__ import annotations

from scripts.evaluate_forward_event_records import evaluate


def test_evaluate_applies_costs() -> None:
    records = [
        {"type": "funding_extreme", "symbol": "BTCUSDT", "raw_return_pct": 1.0, "mfe_pct": 1.5},
        {"type": "funding_extreme", "symbol": "ETHUSDT", "raw_return_pct": -0.1, "mfe_pct": 0.5},
        {"type": "momentum_confirmed", "symbol": "SOLUSDT", "raw_return_pct": 2.0, "mfe_pct": 3.0},
    ]
    report = evaluate(records)
    assert report["records"] == 3
    funding = report["by_type"]["funding_extreme"]
    assert funding["closed"] == 2
    assert funding["win_rate_pct"] == 50.0
    assert funding["net_sum_pct"] == round((1.0 - 0.24) + (-0.1 - 0.24), 3)
    momentum = report["by_type"]["momentum_confirmed"]
    assert momentum["net_sum_pct"] == round(2.0 - 0.60, 3)
