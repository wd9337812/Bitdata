import pandas as pd
import pytest

from scripts import benchmark_s0_pre_funding_capture as subject


def test_estimate_uses_only_bars_visible_before_entry():
    premium = pd.DataFrame(
        {
            "open_time": [0, 300_000, 600_000],
            "close_time": [299_999, 599_999, 899_999],
            "close": [0.001, 0.003, 9.0],
        }
    )
    estimate = subject.estimated_funding(
        premium, funding_time_ms=900_000, interval_hours=1.0
    )
    expected_premium = (0.001 * 1 + 0.003 * 2) / 3
    expected = expected_premium - 0.0005
    assert estimate == pytest.approx(expected)


def test_fast_estimator_matches_reference_estimator():
    premium = pd.DataFrame(
        {
            "open_time": [0, 300_000, 600_000, 900_000],
            "close_time": [299_999, 599_999, 899_999, 1_199_999],
            "close": [0.0002, -0.0001, 0.0004, 0.02],
        }
    )

    reference = subject.estimated_funding(premium, 1_200_000, 8.0)
    fast = subject.estimated_funding_fast(
        subject.premium_estimator_arrays(premium), 1_200_000, 8.0
    )

    assert fast == pytest.approx(reference)


def price_frame(pre_high=101.0, pre_low=99.0, post_open=100.0):
    return pd.DataFrame(
        {
            "open_time": [300_000, 600_000, 900_000],
            "open": [100.0, 100.0, post_open],
            "high": [pre_high, 101.0, post_open],
            "low": [pre_low, 99.0, post_open],
        }
    )


def test_short_receives_positive_funding_only_if_held_at_settlement():
    result = subject.path_result(
        price_frame(), 300_000, 600_000, direction=-1.0, actual_funding=0.001
    )
    assert result["funding_pnl"] == pytest.approx(0.001)
    assert result["gross_return"] == pytest.approx(0.001)

    stopped = subject.path_result(
        price_frame(pre_high=103.0),
        300_000,
        600_000,
        direction=-1.0,
        actual_funding=0.001,
    )
    assert stopped["exit_reason"] == "stop"
    assert stopped["funding_pnl"] == 0.0
    assert stopped["gross_return"] == pytest.approx(-0.02)
