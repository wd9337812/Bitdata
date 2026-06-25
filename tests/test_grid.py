from app.grid import build_grid_plan


def make_bar(open_price: float, high: float, low: float, close: float, ts: int) -> list[float]:
    return [ts, open_price, high, low, close, 0, ts + 1, 0, 0, 0, 0, 0]


def test_grid_plan_ready_for_moderate_volatility():
    bars = []
    price = 100.0
    for i in range(100):
        price += 0.05
        bars.append(make_bar(price, price + 1.0, price - 1.0, price, i))
    plan = build_grid_plan("BTCUSDT", bars, {"grid_min_levels": 20, "grid_max_levels": 80}, 10000)
    assert plan["status"] == "READY"
    assert plan["lower"] < plan["last_price"] < plan["upper"]
