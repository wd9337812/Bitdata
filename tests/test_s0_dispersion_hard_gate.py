from __future__ import annotations

import pandas as pd

from scripts.benchmark_s0_dispersion_hard_gate import (
    apply_hard_gate,
    qualifies,
    terminal_stop_diagnostic,
)


def test_hard_gate_uses_shifted_target_and_drops_high_dispersion_days() -> None:
    days = pd.to_datetime(["2025-01-01", "2025-01-02"], utc=True)
    eligible = pd.DataFrame(
        {"day": [days[0], days[0], days[1]], "symbol": ["A", "B", "A"]}
    )
    dispersion = pd.DataFrame(
        {
            "day": days,
            "dispersion": [0.02, 0.05],
            "dispersion_target": [0.03, 0.03],
        }
    )
    result = apply_hard_gate(eligible, dispersion)
    assert list(result.symbol) == ["A", "B"]
    assert result.gate_open.all()


def test_qualification_requires_every_year_and_window() -> None:
    item = {"profit_factor": 1.1, "total_return_pct": 1.0, "max_drawdown_pct": -20.0}
    evidence = {
        name: {
            "stress_0_12pct_one_way": {
                "unprotected": {
                    **{window: item.copy() for window in (
                        "development_2021_2023", "validation_2024", "test_2025_2026"
                    )},
                    "yearly": {"2021": item.copy(), "2022": item.copy()},
                }
            }
        }
        for name in ("single_position", "single_long_only")
    }
    assert qualifies(evidence)
    for name in evidence:
        evidence[name]["stress_0_12pct_one_way"]["unprotected"]["yearly"][
            "2022"
        ]["profit_factor"] = 0.9
    assert not qualifies(evidence)


def test_terminal_stop_diagnostic_only_clips_losses() -> None:
    daily = pd.DataFrame({"net_return": [-0.5, -0.2, 0.4]})
    result = terminal_stop_diagnostic(daily)
    assert result.net_return.tolist() == [-0.3, -0.2, 0.4]
