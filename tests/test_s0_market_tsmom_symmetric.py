import pandas as pd

from scripts.benchmark_s0_market_tsmom_symmetric import (
    directional_proxy_trades,
    directional_state,
)


def test_directional_state_requires_fast_and_slow_agreement() -> None:
    count = 181
    index = [1.0] * (count - 2) + [1.2, 0.8]
    momentum = [0.0] * (count - 2) + [0.2, -0.2]
    state = pd.DataFrame(
        {
            "market_index": index,
            "momentum_28d": momentum,
            "historical_top_third": [0.1] * count,
            "universe_size": [20] * count,
        }
    )

    result = directional_state(state)

    assert result.iloc[-2].direction == 1
    assert result.iloc[-1].direction == -1


def test_short_trade_inverts_asset_return() -> None:
    days = pd.date_range("2024-01-01", periods=7, freq="D", tz="UTC")
    panel = pd.DataFrame(
        {"symbol": "BTCUSDT", "day": days, "open": [100, 100, 99, 98, 97, 96, 90]}
    )
    state = pd.DataFrame({"day": days, "direction": [-1, 0, 0, 0, 0, 0, 0]})

    trades = directional_proxy_trades(panel, state)

    assert len(trades) == 1
    assert trades.iloc[0].gross_return > 0
