from app import scanner
from app.scanner import active_growth_mode, discover_coin_symbols, latest_strategy_signal, mode_config, scan_growth_candidates


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


def test_mode_config_uses_mode_specific_interval():
    config = {
        "auto_risk_by_equity": False,
        "growth_mode": "attack",
        "attack_interval": "15m",
        "attack_risk_per_trade_pct": 5,
        "attack_max_leverage": 3,
        "attack_max_symbol_margin_pct": 60,
        "attack_recent_days": 10,
    }
    active = mode_config(config, 200)
    assert active["mode"] == "attack"
    assert active["interval"] == "15m"
    assert active["recent_days"] == 10
    assert active["risk_pct"] == 5


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


def test_scan_promotes_high_score_near_trigger_to_preemptive(monkeypatch):
    class FakeScanClient:
        def ticker_24h(self, symbols=None):
            return [{"symbol": "LABUSDT", "lastPrice": "10", "quoteVolume": "800000000", "priceChangePercent": "-10"}]

        def klines_history(self, symbol, interval, days):
            return [[i, 10, 11, 9, 10, 1000] for i in range(120)]

    monkeypatch.setattr(scanner, "discover_coin_symbols", lambda client, config: ["LABUSDT"])
    monkeypatch.setattr(
        scanner,
        "latest_strategy_signal",
        lambda symbol, bars, strategy, params=None, direction="LONG": {
            "symbol": symbol,
            "signal": "WAIT",
            "reason": "filters_not_aligned",
            "strategy": strategy,
            "last_price": 10.0,
            "ema_fast": 9.5,
            "ema_slow": 9.0,
            "atr": 0.1,
            "direction": direction,
            "trend": True,
            "trigger": False,
            "volatility_ok": True,
            "trigger_price": 10.02,
            "distance_to_trigger_pct": 0.2,
            "candle_move_pct": 0.25,
        },
    )
    monkeypatch.setattr(
        scanner,
        "backtest_strategy",
        lambda symbol, bars, strategy, days, direction="LONG": {
            "symbol": symbol,
            "strategy": strategy,
            "direction": direction,
            "days": days,
            "trades": 12,
            "wins": 7,
            "win_rate": 58.3,
            "net_pct": 35,
            "profit_factor": 3.2,
        },
    )

    result = scan_growth_candidates(
        FakeScanClient(),
        {
            "growth_mode": "tournament",
            "auto_risk_by_equity": False,
            "tournament_interval": "5m",
            "tournament_recent_days": 5,
            "tournament_risk_per_trade_pct": 15,
            "tournament_max_leverage": 5,
            "tournament_max_symbol_margin_pct": 90,
            "preemptive_entries_enabled": True,
            "preemptive_min_score": 50,
            "preemptive_max_distance_pct": 0.35,
            "preemptive_risk_multiplier": 0.47,
            "allow_short": False,
            "min_expected_profit_cost_ratio": 2,
            "estimated_slippage_pct": 0.04,
            "min_expected_profit_pct": 0.35,
        },
        {"equity": 50},
    )

    best = result["candidates"][0]
    assert best["passed"] is True
    assert best["entry_type"] in {"preemptive", "momentum"}
    assert best["signal"]["signal"] == "LONG"
    assert best["risk_pct"] < 15
