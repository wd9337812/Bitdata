import pandas as pd

from scripts import benchmark_s0_spot_perp_basis as subject


def test_funding_pnl_short_receives_positive_rate():
    funding = pd.DataFrame(
        {"funding_time": [100, 200, 300], "funding_rate": [0.001, -0.002, 0.003]}
    )
    assert subject.funding_pnl(funding, 100, 300) == 0.001


def test_pair_metrics_divide_return_by_two_legs():
    result = subject.metrics(pd.Series([0.02]))
    assert result["pair_net_return"] == 0.02
    assert result["total_capital_net_return"] == 0.01


def test_candidate_entries_choose_largest_basis_per_hour():
    markets = {
        "A": pd.DataFrame(
            {
                "open_time": [1],
                "basis_close": [0.01],
                "latest_funding": [0.001],
                "spot_liquidity_24h": [30_000_000],
            }
        ),
        "B": pd.DataFrame(
            {
                "open_time": [1],
                "basis_close": [0.02],
                "latest_funding": [0.001],
                "spot_liquidity_24h": [30_000_000],
            }
        ),
    }
    result = subject.candidate_entries(markets, 0.005)
    assert result.iloc[0].symbol == "B"
