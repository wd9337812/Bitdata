from app.strategy import backtest, latest_signal


def make_bar(open_price: float, high: float, low: float, close: float, ts: int) -> list[float]:
    return [ts, open_price, high, low, close, 0, ts + 1, 0, 0, 0, 0, 0]


def test_latest_signal_waits_without_enough_data():
    bars = [make_bar(10, 11, 9, 10, i) for i in range(10)]
    assert latest_signal("SOLUSDT", bars)["signal"] == "WAIT"


def test_backtest_returns_summary_shape():
    bars = []
    price = 100.0
    for i in range(120):
        price += 0.2
        bars.append(make_bar(price, price + 1.5, price - 1.0, price + 0.5, i))
    result = backtest("TESTUSDT", bars)
    assert result["summary"]["symbol"] == "TESTUSDT"
    assert "win_rate" in result["summary"]
