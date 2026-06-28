from app.scanner import active_growth_mode, discover_coin_symbols, latest_strategy_signal


class FakeClient:
    def exchange_info(self):
        return {
            "symbols": [
                {
                    "symbol": "SOLUSDT",
                    "contractType": "PERPETUAL",
                    "underlyingType": "COIN",
                    "status": "TRADING",
                    "quoteAsset": "USDT",
                },
                {
                    "symbol": "MUUSDT",
                    "contractType": "TRADIFI_PERPETUAL",
                    "underlyingType": "EQUITY",
                    "status": "TRADING",
                    "quoteAsset": "USDT",
                },
            ]
        }

    def ticker_24h(self, symbols=None):
        return [
            {"symbol": "SOLUSDT", "quoteVolume": "200000000"},
            {"symbol": "MUUSDT", "quoteVolume": "999000000"},
        ]


def make_bar(open_price: float, high: float, low: float, close: float, ts: int) -> list[float]:
    return [ts, open_price, high, low, close, 0, ts + 1, 0, 0, 0, 0, 0]


def test_auto_growth_mode_uses_tournament_for_small_equity():
    assert active_growth_mode({"auto_risk_by_equity": True, "growth_mode": "balanced"}, 50) == "tournament"
    assert active_growth_mode({"auto_risk_by_equity": True, "growth_mode": "balanced"}, 200) == "attack"
    assert active_growth_mode({"auto_risk_by_equity": True, "growth_mode": "balanced"}, 1000) == "balanced"


def test_discover_coin_symbols_excludes_equity_contracts():
    symbols = discover_coin_symbols(
        FakeClient(),
        {"auto_discover_symbols": True, "stage1_symbols": ["MUUSDT", "SOLUSDT"], "min_24h_volume_usdt": 1, "max_scan_symbols": 10},
    )
    assert symbols == ["SOLUSDT"]


def test_latest_attack_signal_shape_wait_or_long():
    bars = []
    price = 100.0
    for i in range(120):
        price += 0.1
        bars.append(make_bar(price, price + 1.0, price - 0.8, price + 0.2, i))
    signal = latest_strategy_signal("TESTUSDT", bars, "attack")
    assert signal["signal"] in {"WAIT", "LONG"}
    assert signal["strategy"] == "attack"
