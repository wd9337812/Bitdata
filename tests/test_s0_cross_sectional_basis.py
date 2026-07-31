import pandas as pd

from scripts import benchmark_s0_cross_sectional_basis as subject


def test_select_candidates_uses_high_basis_for_long():
    panel = pd.DataFrame(
        {
            "day": [pd.Timestamp("2026-01-01", tz="UTC")] * 2,
            "symbol": ["A", "B"],
            "basis_factor": [0.01, -0.02],
            "median_basis": [-0.005, -0.005],
            "distance": [0.015, 0.015],
        }
    )
    result = subject.select_candidates(panel, "long_high")
    assert result.iloc[0].symbol == "A"
    assert result.iloc[0].side == "long"


def test_select_candidates_uses_low_basis_for_short():
    panel = pd.DataFrame(
        {
            "day": [pd.Timestamp("2026-01-01", tz="UTC")] * 2,
            "symbol": ["A", "B"],
            "basis_factor": [0.01, -0.02],
            "median_basis": [-0.005, -0.005],
            "distance": [0.015, 0.015],
        }
    )
    result = subject.select_candidates(panel, "short_low")
    assert result.iloc[0].symbol == "B"
    assert result.iloc[0].side == "short"


def test_metrics_removes_top_three_winners():
    result = subject.metrics(pd.Series([1.0, 0.8, 0.6, 0.1, -0.2]))
    assert result["profit_factor"] > 1
    assert result["without_top3_profit_factor"] == 0.5
