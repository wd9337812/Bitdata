import pandas as pd

from scripts.benchmark_s0_market_tsmom_28d import (
    btc_proxy_trades,
    qualifies,
    market_basket_trades,
    select_proxy_on_training_period,
)


def test_btc_proxy_enters_next_day_and_prevents_overlap() -> None:
    days = pd.date_range("2024-01-01", periods=12, freq="D", tz="UTC")
    panel = pd.DataFrame(
        {"symbol": "BTCUSDT", "day": days, "open": range(100, 112)}
    )
    state = pd.DataFrame(
        {
            "day": days,
            "signal": [True] * 12,
            "momentum_28d": 0.2,
            "historical_top_third": 0.1,
        }
    )

    trades = btc_proxy_trades(panel, state)

    assert trades.entry_day.tolist() == [days[1], days[6]]
    assert trades.exit_day.tolist() == [days[6], days[11]]


def test_qualification_requires_each_year_positive() -> None:
    annual = {
        str(year): {"net_return": 0.1, "profit_factor": 1.2}
        for year in range(2021, 2027)
    }
    stress = {
        "overall": {"profit_factor": 1.2, "max_drawdown": -0.4},
        "annual": annual,
    }
    assert qualifies(stress)
    stress["annual"]["2024"]["net_return"] = -0.1
    assert not qualifies(stress)


def test_proxy_selection_uses_training_period_only() -> None:
    btc = pd.DataFrame(
        {
            "entry_day": pd.to_datetime(["2023-01-01", "2024-01-01"], utc=True),
            "gross_return": [0.20, -0.90],
        }
    )
    eth = pd.DataFrame(
        {
            "entry_day": pd.to_datetime(["2023-01-01", "2024-01-01"], utc=True),
            "gross_return": [0.10, 9.00],
        }
    )

    selected = select_proxy_on_training_period(
        {"BTCUSDT": btc, "ETHUSDT": eth}, one_way_cost=0.0
    )

    assert selected == "BTCUSDT"


def test_market_basket_uses_signal_day_constituents_and_next_open() -> None:
    days = pd.date_range("2024-01-01", periods=8, freq="D", tz="UTC")
    rows = []
    for index in range(20):
        symbol = f"S{index:02d}USDT"
        for day_index, day in enumerate(days):
            rows.append(
                {
                    "day": day,
                    "symbol": symbol,
                    "open": 100.0 + day_index,
                    "median_volume_30d": 1000.0 - index,
                }
            )
    panel = pd.DataFrame(rows)
    state = pd.DataFrame({"day": days, "signal": [True] + [False] * 7})

    trades = market_basket_trades(panel, state)

    assert len(trades) == 1
    assert trades.iloc[0].entry_day == days[1]
    assert trades.iloc[0].exit_day == days[6]
    assert trades.iloc[0].constituents == 20
    assert trades.iloc[0].gross_return == (106.0 / 101.0 - 1.0)
