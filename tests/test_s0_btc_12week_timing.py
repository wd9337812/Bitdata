import pandas as pd
import pytest

from scripts.benchmark_s0_btc_12week_timing import qualifies, strategy_returns


def test_signal_is_delayed_one_day_and_costs_only_turnover() -> None:
    daily = pd.DataFrame(
        {
            "day": pd.date_range("2024-01-01", periods=4, freq="D", tz="UTC"),
            "asset_return": [0.0, 0.1, 0.1, 0.1],
            "momentum_12week": [0.2, 0.2, -0.2, -0.2],
        }
    )

    frame = strategy_returns(daily, 0.01, allow_short=False)

    assert frame.position.tolist() == [0.0, 1.0, 1.0, 0.0]
    assert frame.turnover.tolist() == [0.0, 1.0, 0.0, 1.0]
    assert frame.net_return.tolist() == pytest.approx([0.0, 0.09, 0.1, -0.01])


def test_long_short_uses_negative_signal_on_following_day() -> None:
    daily = pd.DataFrame(
        {
            "day": pd.date_range("2024-01-01", periods=3, freq="D", tz="UTC"),
            "asset_return": [0.0, 0.1, -0.1],
            "momentum_12week": [-0.2, -0.2, 0.2],
        }
    )

    frame = strategy_returns(daily, 0.0, allow_short=True)

    assert frame.position.tolist() == [0.0, -1.0, -1.0]
    assert frame.net_return.tolist() == [0.0, -0.1, 0.1]


def test_qualification_requires_every_year_positive() -> None:
    annual = {str(year): {"net_return": 0.1} for year in range(2021, 2027)}
    report = {
        "overall": {"episode_profit_factor": 1.2, "max_drawdown": -0.4},
        "annual": annual,
    }
    assert qualifies(report)
    report["annual"]["2026"]["net_return"] = 0.0
    assert not qualifies(report)
