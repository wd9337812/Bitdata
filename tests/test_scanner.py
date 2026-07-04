from app import scanner
from app.scanner import active_growth_mode, discover_coin_symbols, latest_strategy_signal, mode_config, scan_growth_candidates, score_symbol_quality


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


def test_auto_growth_mode_uses_sprint_when_enabled_for_small_equity():
    assert active_growth_mode(
        {
            "auto_risk_by_equity": True,
            "growth_mode": "balanced",
            "tournament_sprint_enabled": True,
            "tournament_sprint_auto_under_equity": 100,
        },
        69,
    ) == "tournament_sprint"


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


def test_sprint_quality_promotes_hot_observe_with_risk_discount():
    bars = [make_bar(10, 10.2, 9.8, 10, 100, ) for _ in range(25)]
    for index, bar in enumerate(bars):
        bar[5] = 100
        bar[0] = index
    bars[-1][5] = 400
    quality = score_symbol_quality(
        "HOTUSDT",
        bars,
        {"quoteVolume": "800000000", "lastPrice": "10"},
        {
            "symbol": "HOTUSDT",
            "signal": "WAIT",
            "last_price": 10.0,
            "atr": 0.8,
            "trend": True,
        },
        {
            3: {"trades": 2, "win_rate": 50, "profit_factor": 1.2, "net_pct": 3},
            5: {"trades": 2, "win_rate": 50, "profit_factor": 1.0, "net_pct": 1},
        },
        {"spread_pct": 0.02, "depth_notional": 60_000},
        {
            "growth_mode": "tournament_sprint",
            "volume_spike_ratio": 1.8,
            "min_simulated_trades": 5,
            "min_depth_notional_usdt": 20_000,
            "max_spread_pct": 0.08,
            "sprint_symbol_hot_observe_score": 45,
            "sprint_sample_penalty_exempt_spike": 2.5,
            "sprint_hot_observe_risk_multiplier": 0.35,
            "sprint_high_atr_risk_multiplier": 0.6,
        },
        {"mode": "tournament_sprint"},
    )

    assert quality["pool"] == "observe_hot"
    assert quality["allowed"] is True
    assert quality["quality_risk_multiplier"] < 1
    assert "热点观察" in "；".join(quality["quality_risk_reasons"])


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

        def depth(self, symbol, limit=5):
            return {"bids": [["9.999", "5000"]], "asks": [["10.001", "5000"]]}

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
            "min_depth_notional_usdt": 20_000,
        },
        {"equity": 50},
    )

    best = result["candidates"][0]
    assert best["passed"] is True
    assert best["entry_type"] in {"preemptive", "momentum"}
    assert best["signal"]["signal"] == "LONG"
    assert best["risk_pct"] < 15


def test_sprint_promotes_looser_near_trigger_to_preemptive(monkeypatch):
    class FakeScanClient:
        def ticker_24h(self, symbols=None):
            return [{"symbol": "LABUSDT", "lastPrice": "10", "quoteVolume": "800000000", "priceChangePercent": "12"}]

        def klines_history(self, symbol, interval, days):
            return [[i, 10, 11, 9, 10, 1000] for i in range(120)]

        def depth(self, symbol, limit=5):
            return {"bids": [["9.999", "5000"]], "asks": [["10.001", "5000"]]}

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
            "trigger_price": 10.045,
            "distance_to_trigger_pct": 0.45,
            "candle_move_pct": 0.46,
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
            "trades": 3,
            "wins": 1,
            "win_rate": 33.3,
            "net_pct": -1.0,
            "profit_factor": 1.05,
        },
    )
    monkeypatch.setattr(
        scanner,
        "score_symbol_quality",
        lambda symbol, bars, ticker, signal, backtests, depth, config, mode=None: {
            "score": 80,
            "pool": "trade",
            "allowed": True,
            "components": {},
            "market_passed": True,
            "simulation": {"passed": True},
            "market": {"atr_pct": 1.0, "spread_pct": 0.02, "depth_notional": 50_000},
        },
    )

    result = scan_growth_candidates(
        FakeScanClient(),
        {
            "growth_mode": "tournament_sprint",
            "auto_risk_by_equity": False,
            "tournament_sprint_interval": "5m",
            "tournament_sprint_recent_days": 3,
            "tournament_sprint_risk_per_trade_pct": 18,
            "tournament_sprint_max_leverage": 5,
            "tournament_sprint_max_symbol_margin_pct": 95,
            "tournament_sprint_long_min_profit_factor": 0.85,
            "tournament_sprint_long_min_net_pct": -3,
            "preemptive_entries_enabled": True,
            "tournament_sprint_preemptive_min_score": 40,
            "tournament_sprint_preemptive_max_distance_pct": 0.55,
            "tournament_sprint_preemptive_risk_multiplier": 0.35,
            "tournament_sprint_min_expected_profit_cost_ratio": 1.2,
            "tournament_sprint_min_expected_profit_pct": 0.2,
            "allow_short": False,
            "estimated_slippage_pct": 0.04,
            "min_depth_notional_usdt": 20_000,
        },
        {"equity": 69},
    )

    best = result["candidates"][0]
    assert result["mode"]["mode"] == "tournament_sprint"
    assert best["passed"] is True
    assert best["entry_type"] in {"preemptive", "momentum"}
    assert best["risk_pct"] < 18


def test_scan_blocks_signal_when_symbol_quality_fails(monkeypatch):
    class FakeScanClient:
        def ticker_24h(self, symbols=None):
            return [{"symbol": "ZBTUSDT", "lastPrice": "0.15", "quoteVolume": "1000000", "priceChangePercent": "35"}]

        def klines_history(self, symbol, interval, days):
            return [[i, 0.15, 0.16, 0.14, 0.15, 100] for i in range(200)]

    monkeypatch.setattr(scanner, "discover_coin_symbols", lambda client, config: ["ZBTUSDT"])
    monkeypatch.setattr(
        scanner,
        "latest_strategy_signal",
        lambda symbol, bars, strategy, params=None, direction="LONG": {
            "symbol": symbol,
            "signal": direction,
            "strategy": strategy,
            "last_price": 0.15,
            "atr": 0.002,
            "stop": 0.145,
            "take_profit": 0.16,
            "expected_profit_pct": 6.0,
            "trend": True,
            "volatility_ok": True,
            "entry_type": "standard",
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
            "trades": 1,
            "wins": 1,
            "win_rate": 100.0,
            "net_pct": 0.3,
            "profit_factor": 999,
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
            "allow_short": False,
            "min_expected_profit_cost_ratio": 2,
            "estimated_slippage_pct": 0.04,
            "min_expected_profit_pct": 0.35,
            "standard_min_score": 50,
            "symbol_trade_score": 75,
            "symbol_small_trade_score": 65,
            "symbol_observe_score": 50,
            "min_simulated_trades": 5,
            "quality_backtest_days": [3, 5, 10],
            "min_depth_notional_usdt": 5_000,
        },
        {"equity": 50},
    )

    best = result["candidates"][0]
    assert best["signal"]["signal"] == "LONG"
    assert best["passed"] is False
    assert best["symbol_quality"]["allowed"] is False
    assert "币种质量未达实盘准入" in best["decision_reason"]


def test_observe_high_score_standard_breakout_uses_discount_risk(monkeypatch):
    class FakeScanClient:
        def ticker_24h(self, symbols=None):
            return [{"symbol": "INUSDT", "lastPrice": "0.06", "quoteVolume": "80000000", "priceChangePercent": "8"}]

        def klines_history(self, symbol, interval, days):
            return [[i, 0.06, 0.063, 0.058, 0.06, 1000] for i in range(300)]

        def depth(self, symbol, limit=5):
            return {"bids": [["0.05999", "100000"]], "asks": [["0.06001", "100000"]]}

    monkeypatch.setattr(scanner, "discover_coin_symbols", lambda client, config: ["INUSDT"])
    monkeypatch.setattr(
        scanner,
        "latest_strategy_signal",
        lambda symbol, bars, strategy, params=None, direction="LONG": {
            "symbol": symbol,
            "signal": direction,
            "strategy": strategy,
            "last_price": 0.06,
            "atr": 0.002,
            "stop": 0.0576,
            "take_profit": 0.066,
            "expected_profit_pct": 10.0,
            "trend": True,
            "volatility_ok": True,
            "entry_type": "standard",
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
            "trades": 18,
            "wins": 10,
            "win_rate": 55.5,
            "net_pct": 30.0,
            "profit_factor": 2.5,
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
            "allow_short": False,
            "min_expected_profit_cost_ratio": 3,
            "estimated_slippage_pct": 0.04,
            "min_expected_profit_pct": 0.35,
            "standard_min_score": 85,
            "symbol_trade_score": 95,
            "symbol_small_trade_score": 90,
            "symbol_observe_score": 50,
            "min_simulated_trades": 5,
            "quality_backtest_days": [3, 5, 10],
            "min_depth_notional_usdt": 20_000,
            "depth_check_top_symbols": 1,
            "observe_breakout_enabled": True,
            "observe_breakout_min_score": 85,
            "observe_breakout_min_quality": 70,
            "observe_breakout_min_cost_ratio": 20,
            "observe_breakout_min_profit_factor": 1.5,
            "observe_breakout_min_net_pct": 4,
            "observe_breakout_min_depth_notional_usdt": 500,
            "observe_breakout_max_spread_pct": 0.08,
            "observe_breakout_risk_multiplier": 0.22,
        },
        {"equity": 50},
    )

    best = result["candidates"][0]
    assert best["passed"] is True
    assert best["entry_type"] == "observe_standard"
    assert best["symbol_pool"] == "observe"
    assert best["risk_pct"] == 2.475
    assert best["risk_adjustment"]["multiplier"] == 0.165
    assert "观察池高分标准突破" in best["decision_reason"]


def test_observe_breakout_reduces_risk_after_consecutive_live_losses(monkeypatch):
    class FakeScanClient:
        api_key = "key"
        api_secret = "secret"

        def ticker_24h(self, symbols=None):
            return [{"symbol": "TLMUSDT", "lastPrice": "0.002", "quoteVolume": "360000000", "priceChangePercent": "20"}]

        def klines_history(self, symbol, interval, days):
            return [[i, 0.002, 0.0022, 0.0019, 0.002, 100000] for i in range(300)]

        def depth(self, symbol, limit=5):
            return {"bids": [["0.0019995", "5000000"]], "asks": [["0.0020005", "5000000"]]}

        def income_history(self, limit=100, income_type=None):
            return [
                {"symbol": "TLMUSDT", "incomeType": "REALIZED_PNL", "income": "-6.0", "time": 3000},
                {"symbol": "TAIKOUSDT", "incomeType": "REALIZED_PNL", "income": "-1.5", "time": 2000},
                {"symbol": "LABUSDT", "incomeType": "REALIZED_PNL", "income": "1.0", "time": 1000},
            ]

    monkeypatch.setattr(scanner, "discover_coin_symbols", lambda client, config: ["TLMUSDT"])
    monkeypatch.setattr(
        scanner,
        "latest_strategy_signal",
        lambda symbol, bars, strategy, params=None, direction="LONG": {
            "symbol": symbol,
            "signal": direction,
            "strategy": strategy,
            "last_price": 0.002,
            "atr": 0.00008,
            "stop": 0.0019,
            "take_profit": 0.0022,
            "expected_profit_pct": 10.0,
            "trend": True,
            "volatility_ok": True,
            "entry_type": "standard",
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
            "trades": 20,
            "wins": 12,
            "win_rate": 60.0,
            "net_pct": 40.0,
            "profit_factor": 3.0,
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
            "allow_short": False,
            "min_expected_profit_cost_ratio": 3,
            "estimated_slippage_pct": 0.04,
            "min_expected_profit_pct": 0.35,
            "standard_min_score": 85,
            "symbol_trade_score": 95,
            "symbol_small_trade_score": 90,
            "symbol_observe_score": 50,
            "min_simulated_trades": 5,
            "quality_backtest_days": [3, 5, 10],
            "min_depth_notional_usdt": 20_000,
            "depth_check_top_symbols": 1,
            "observe_breakout_enabled": True,
            "observe_breakout_min_score": 85,
            "observe_breakout_min_quality": 70,
            "observe_breakout_min_cost_ratio": 20,
            "observe_breakout_min_profit_factor": 1.5,
            "observe_breakout_min_net_pct": 4,
            "observe_breakout_min_depth_notional_usdt": 500,
            "observe_breakout_max_spread_pct": 0.08,
            "observe_breakout_risk_multiplier": 0.22,
            "observe_low_price_threshold": 0.01,
            "observe_low_price_risk_multiplier": 0.75,
            "observe_high_atr_pct": 3.0,
            "observe_high_atr_risk_multiplier": 0.75,
            "observe_consecutive_loss_count": 2,
            "observe_consecutive_loss_risk_multiplier": 0.5,
            "observe_extreme_atr_pct": 3.0,
            "observe_extreme_depth_notional_usdt": 5_000,
        },
        {"equity": 50},
    )

    best = result["candidates"][0]
    assert best["passed"] is True
    assert best["entry_type"] == "observe_standard"
    assert round(best["risk_pct"], 6) == round(15 * 0.22 * 0.75 * 0.75 * 0.5, 6)
    assert best["risk_adjustment"]["live_losses"]["count"] == 2


def test_observe_breakout_blocks_extreme_atr_with_weak_depth(monkeypatch):
    class FakeScanClient:
        def ticker_24h(self, symbols=None):
            return [{"symbol": "TLMUSDT", "lastPrice": "0.002", "quoteVolume": "360000000", "priceChangePercent": "20"}]

        def klines_history(self, symbol, interval, days):
            return [[i, 0.002, 0.0022, 0.0019, 0.002, 100000] for i in range(300)]

        def depth(self, symbol, limit=5):
            return {"bids": [["0.001999", "1000000"]], "asks": [["0.002001", "1000000"]]}

    monkeypatch.setattr(scanner, "discover_coin_symbols", lambda client, config: ["TLMUSDT"])
    monkeypatch.setattr(
        scanner,
        "latest_strategy_signal",
        lambda symbol, bars, strategy, params=None, direction="LONG": {
            "symbol": symbol,
            "signal": direction,
            "strategy": strategy,
            "last_price": 0.002,
            "atr": 0.00008,
            "stop": 0.0019,
            "take_profit": 0.0022,
            "expected_profit_pct": 10.0,
            "trend": True,
            "volatility_ok": True,
            "entry_type": "standard",
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
            "trades": 20,
            "wins": 12,
            "win_rate": 60.0,
            "net_pct": 40.0,
            "profit_factor": 3.0,
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
            "allow_short": False,
            "min_expected_profit_cost_ratio": 3,
            "estimated_slippage_pct": 0.04,
            "min_expected_profit_pct": 0.35,
            "standard_min_score": 85,
            "symbol_trade_score": 95,
            "symbol_small_trade_score": 90,
            "symbol_observe_score": 50,
            "min_simulated_trades": 5,
            "quality_backtest_days": [3, 5, 10],
            "min_depth_notional_usdt": 20_000,
            "depth_check_top_symbols": 1,
            "observe_breakout_enabled": True,
            "observe_breakout_min_score": 85,
            "observe_breakout_min_quality": 70,
            "observe_breakout_min_cost_ratio": 20,
            "observe_breakout_min_profit_factor": 1.5,
            "observe_breakout_min_net_pct": 4,
            "observe_breakout_min_depth_notional_usdt": 500,
            "observe_breakout_max_spread_pct": 0.08,
            "observe_extreme_atr_pct": 3.0,
            "observe_extreme_depth_notional_usdt": 5_000,
        },
        {"equity": 50},
    )

    best = result["candidates"][0]
    assert best["passed"] is False
    assert best["entry_type"] == "watch"


def test_live_performance_promotes_same_symbol_direction_to_adaptive_pool(monkeypatch):
    class FakeScanClient:
        api_key = "key"
        api_secret = "secret"

        def ticker_24h(self, symbols=None):
            return [{"symbol": "INUSDT", "lastPrice": "0.056", "quoteVolume": "92000000", "priceChangePercent": "-15"}]

        def klines_history(self, symbol, interval, days):
            return [[i, 0.056, 0.057, 0.055, 0.056, 1000] for i in range(300)]

        def depth(self, symbol, limit=5):
            return {"bids": [["0.05599", "30000"]], "asks": [["0.05601", "30000"]]}

        def user_trades(self, symbol, limit=100):
            now = int(scanner.time.time() * 1000)
            return [
                {
                    "symbol": symbol,
                    "orderId": 1,
                    "positionSide": "SHORT",
                    "time": now - 30_000,
                    "realizedPnl": "-4.4",
                    "commission": "0.05",
                    "commissionAsset": "USDT",
                    "quoteQty": "300",
                },
                {
                    "symbol": symbol,
                    "orderId": 2,
                    "positionSide": "SHORT",
                    "time": now - 20_000,
                    "realizedPnl": "8.8",
                    "commission": "0.08",
                    "commissionAsset": "USDT",
                    "quoteQty": "480",
                },
                {
                    "symbol": symbol,
                    "orderId": 3,
                    "positionSide": "SHORT",
                    "time": now - 10_000,
                    "realizedPnl": "-2.4",
                    "commission": "0.04",
                    "commissionAsset": "USDT",
                    "quoteQty": "170",
                },
            ]

    monkeypatch.setattr(scanner, "discover_coin_symbols", lambda client, config: ["INUSDT"])
    monkeypatch.setattr(
        scanner,
        "latest_strategy_signal",
        lambda symbol, bars, strategy, params=None, direction="LONG": {
            "symbol": symbol,
            "signal": direction,
            "strategy": strategy,
            "last_price": 0.056,
            "atr": 0.0005,
            "stop": 0.057,
            "take_profit": 0.054,
            "expected_profit_pct": 3.2,
            "trend": True,
            "volatility_ok": True,
            "entry_type": "standard",
        } if direction == "SHORT" else {"symbol": symbol, "signal": "WAIT", "last_price": 0.056},
    )
    monkeypatch.setattr(
        scanner,
        "backtest_strategy",
        lambda symbol, bars, strategy, days, direction="LONG": {
            "symbol": symbol,
            "strategy": strategy,
            "direction": direction,
            "days": days,
            "trades": 22,
            "wins": 8,
            "win_rate": 36.36,
            "net_pct": 41.2,
            "profit_factor": 3.34,
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
            "allow_short": True,
            "short_risk_multiplier": 0.5,
            "short_min_recent_trades": 5,
            "short_min_profit_factor": 1.3,
            "short_min_net_pct": 1.0,
            "min_expected_profit_cost_ratio": 2,
            "estimated_slippage_pct": 0.04,
            "min_expected_profit_pct": 0.35,
            "standard_min_score": 50,
            "symbol_trade_score": 75,
            "symbol_small_trade_score": 65,
            "symbol_observe_score": 50,
            "min_simulated_trades": 5,
            "min_simulated_win_rate": 45,
            "min_simulated_profit_factor": 1.25,
            "min_simulated_net_pct": 1.5,
            "quality_backtest_days": [3, 5],
            "min_depth_notional_usdt": 20_000,
            "live_performance_min_depth_notional_usdt": 1_500,
            "live_performance_risk_multiplier": 0.6,
        },
        {"equity": 72},
    )

    best = result["candidates"][0]
    assert best["direction"] == "SHORT"
    assert best["passed"] is True
    assert best["symbol_pool"] == "adaptive_live"
    assert best["live_performance"]["passed"] is True
    assert best["risk_pct"] == 4.5


def test_small_trade_pool_reduces_risk(monkeypatch):
    class FakeScanClient:
        def ticker_24h(self, symbols=None):
            return [{"symbol": "TESTUSDT", "lastPrice": "10", "quoteVolume": "50000000", "priceChangePercent": "5"}]

        def klines_history(self, symbol, interval, days):
            return [[i, 10, 10.4, 9.8, 10, 1000] for i in range(200)]

        def depth(self, symbol, limit=5):
            return {"bids": [["9.999", "1000"]], "asks": [["10.001", "1000"]]}

    monkeypatch.setattr(scanner, "discover_coin_symbols", lambda client, config: ["TESTUSDT"])
    monkeypatch.setattr(
        scanner,
        "latest_strategy_signal",
        lambda symbol, bars, strategy, params=None, direction="LONG": {
            "symbol": symbol,
            "signal": direction,
            "strategy": strategy,
            "last_price": 10.0,
            "atr": 0.1,
            "stop": 9.8,
            "take_profit": 10.5,
            "expected_profit_pct": 5.0,
            "trend": True,
            "volatility_ok": True,
            "entry_type": "standard",
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
            "trades": 8,
            "wins": 4,
            "win_rate": 50.0,
            "net_pct": 2.0,
            "profit_factor": 1.3,
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
            "allow_short": False,
            "min_expected_profit_cost_ratio": 2,
            "estimated_slippage_pct": 0.04,
            "min_expected_profit_pct": 0.35,
            "standard_min_score": 50,
            "symbol_trade_score": 90,
            "symbol_small_trade_score": 50,
            "symbol_observe_score": 40,
            "small_trade_risk_multiplier": 0.5,
            "quality_backtest_days": [3, 5, 10],
            "min_depth_notional_usdt": 5_000,
        },
        {"equity": 50},
    )

    best = result["candidates"][0]
    assert best["passed"] is True
    assert best["symbol_pool"] == "small_trade"
    assert best["risk_pct"] == 7.5


def test_depth_checks_are_rate_limited(monkeypatch):
    class FakeScanClient:
        def __init__(self):
            self.depth_calls = 0

        def ticker_24h(self, symbols=None):
            return [
                {"symbol": symbol, "lastPrice": "10", "quoteVolume": "50000000", "priceChangePercent": "5"}
                for symbol in ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
            ]

        def klines_history(self, symbol, interval, days):
            return [[i, 10, 10.4, 9.8, 10, 1000] for i in range(200)]

        def depth(self, symbol, limit=5):
            self.depth_calls += 1
            return {"bids": [["9.999", "1000"]], "asks": [["10.001", "1000"]]}

    monkeypatch.setattr(scanner, "discover_coin_symbols", lambda client, config: ["AAAUSDT", "BBBUSDT", "CCCUSDT"])
    monkeypatch.setattr(
        scanner,
        "latest_strategy_signal",
        lambda symbol, bars, strategy, params=None, direction="LONG": {
            "symbol": symbol,
            "signal": direction,
            "strategy": strategy,
            "last_price": 10.0,
            "atr": 0.1,
            "stop": 9.8,
            "take_profit": 10.5,
            "expected_profit_pct": 5.0,
            "trend": True,
            "volatility_ok": True,
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
            "trades": 8,
            "wins": 4,
            "win_rate": 50.0,
            "net_pct": 2.0,
            "profit_factor": 1.3,
        },
    )

    client = FakeScanClient()
    result = scan_growth_candidates(
        client,
        {
            "growth_mode": "tournament",
            "auto_risk_by_equity": False,
            "tournament_interval": "5m",
            "tournament_recent_days": 5,
            "tournament_risk_per_trade_pct": 15,
            "tournament_max_leverage": 5,
            "tournament_max_symbol_margin_pct": 90,
            "allow_short": False,
            "min_expected_profit_cost_ratio": 2,
            "estimated_slippage_pct": 0.04,
            "min_expected_profit_pct": 0.35,
            "standard_min_score": 50,
            "symbol_trade_score": 75,
            "symbol_small_trade_score": 65,
            "symbol_observe_score": 50,
            "min_simulated_trades": 5,
            "quality_backtest_days": [3, 5],
            "min_depth_notional_usdt": 5_000,
            "depth_check_top_symbols": 1,
        },
        {"equity": 50},
    )

    assert client.depth_calls == 1
    assert sum(1 for candidate in result["candidates"] if candidate["depth_checked"]) == 1
    assert sum(1 for candidate in result["candidates"] if candidate["passed"]) == 1
