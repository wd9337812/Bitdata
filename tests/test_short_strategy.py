from __future__ import annotations

from app.scanner import backtest_strategy, latest_strategy_signal
from app.trading_engine import execute_stage1_market_order


def make_short_breakout_bars() -> list[list[float]]:
    bars: list[list[float]] = []
    price = 120.0
    ts = 1_700_000_000_000
    for i in range(120):
        price -= 0.2
        open_price = price + 0.15
        close = price
        high = open_price + 0.2
        low = close - 0.2
        if i == 119:
            close = price - 3.0
            low = close - 0.8
            high = price + 0.1
        bars.append([ts + i * 300_000, open_price, high, low, close, 1000])
    return bars


def test_latest_strategy_signal_can_emit_short_breakout():
    signal = latest_strategy_signal("TESTUSDT", make_short_breakout_bars(), "breakout", direction="SHORT")

    assert signal["signal"] == "SHORT"
    assert signal["stop"] > signal["last_price"]
    assert signal["take_profit"] < signal["last_price"]
    assert signal["expected_profit_pct"] > 0


def test_backtest_strategy_supports_short_direction():
    result = backtest_strategy("TESTUSDT", make_short_breakout_bars(), "breakout", days=3, direction="SHORT")

    assert result["direction"] == "SHORT"
    assert result["trades"] >= 0
    assert "net_pct" in result


class FakeFiltersClient:
    def __init__(self) -> None:
        self.orders: list[tuple[str, str, float | None]] = []

    def exchange_info(self):
        return {
            "symbols": [
                {
                    "symbol": "TESTUSDT",
                    "filters": [
                        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }
            ]
        }

    def set_leverage(self, symbol: str, leverage: int):
        self.orders.append(("LEVERAGE", symbol, float(leverage)))

    def position_side_dual(self):
        return {"dualSidePosition": True}

    def place_market_order(self, symbol: str, side: str, quantity: float, reduce_only: bool = False, position_side: str | None = None):
        self.orders.append(("MARKET", side, quantity, position_side))
        return {"side": side, "quantity": quantity, "positionSide": position_side}

    def place_stop_market(self, symbol: str, side: str, stop_price: float, close_position: bool = True, position_side: str | None = None):
        self.orders.append(("STOP", side, stop_price, position_side))
        return {"side": side, "stopPrice": stop_price, "positionSide": position_side}

    def place_take_profit_market(self, symbol: str, side: str, stop_price: float, close_position: bool = True, position_side: str | None = None):
        self.orders.append(("TAKE_PROFIT", side, stop_price, position_side))
        return {"side": side, "stopPrice": stop_price, "positionSide": position_side}


def test_execute_short_uses_sell_entry_and_buy_protection():
    client = FakeFiltersClient()
    decision = {
        "symbol": "TESTUSDT",
        "action": "OPEN_SHORT",
        "direction": "SHORT",
        "quantity": 1.0,
        "leverage": 3,
        "signal": {"last_price": 100.0, "stop": 102.0, "take_profit": 96.0},
    }

    result = execute_stage1_market_order(
        client,
        decision,
        {"dry_run": False, "live_trading_enabled": True, "live_trading_confirmation": "ENABLE_LIVE_TRADING"},
    )

    assert result["mode"] == "live"
    assert ("MARKET", "SELL", 1.0, "SHORT") in client.orders
    assert any(order[0] == "STOP" and order[1] == "BUY" and order[3] == "SHORT" for order in client.orders)
    assert any(order[0] == "TAKE_PROFIT" and order[1] == "BUY" and order[3] == "SHORT" for order in client.orders)
