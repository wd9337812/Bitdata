from __future__ import annotations

import time

from app.opportunity_engine import (
    build_market_context,
    build_v31_challenger,
    build_v31_medium_context,
    build_v3_signal,
    score_v3_opportunity,
)
from app import scanner


def _ticker(symbol: str, change: float, volume: float = 100_000_000) -> dict:
    return {
        "symbol": symbol,
        "lastPrice": "1",
        "priceChangePercent": str(change),
        "quoteVolume": str(volume),
    }


def _trend_bars(count: int = 100) -> list[list[object]]:
    rows: list[list[object]] = []
    start = int(time.time() * 1000) - count * 300_000
    price = 1.0
    for index in range(count):
        open_price = price
        close = price * 1.002
        high = close * 1.001
        low = open_price * 0.999
        volume = 100_000.0
        rows.append(
            [
                start + index * 300_000,
                str(open_price),
                str(high),
                str(low),
                str(close),
                str(volume),
                start + (index + 1) * 300_000 - 1,
                str(volume * close),
                100,
                str(volume * close * 0.62),
                str(volume * close * 0.62),
                "0",
            ]
        )
        price = close
    previous_high = max(float(row[2]) for row in rows[-49:-1])
    rows[-1][1] = str(previous_high * 0.999)
    rows[-1][2] = str(previous_high * 1.015)
    rows[-1][3] = str(previous_high * 0.997)
    rows[-1][4] = str(previous_high * 1.012)
    rows[-1][5] = "500000"
    rows[-1][7] = str(500_000 * previous_high)
    rows[-1][10] = str(500_000 * previous_high * 0.68)
    return rows


def test_market_context_detects_broad_up_and_cross_sectional_strength():
    rows = [_ticker("BTCUSDT", 3), _ticker("ETHUSDT", 4)]
    rows.extend(_ticker(f"ALT{i}USDT", 2 + i * 0.2) for i in range(8))
    context = build_market_context(rows, {})

    assert context["regime"] == "broad_up"
    assert context["breadth_positive"] == 1
    assert context["symbols"]["ALT7USDT"]["long_strength_percentile"] > 0.8
    assert context["direction_multipliers"]["SHORT"] < context["direction_multipliers"]["LONG"]


def test_v3_signal_recognizes_volume_confirmed_breakout():
    signal = build_v3_signal("ALTUSDT", _trend_bars(), "LONG", {})

    assert signal["signal"] == "LONG"
    assert signal["entry_type"] == "v3_breakout"
    assert signal["donchian_votes"] >= 1
    assert signal["volume_acceleration"] > 1.05
    assert signal["stop"] < signal["last_price"] < signal["take_profit"]


def test_v3_a_plus_requires_structure_liquidity_and_cost_room():
    context = {
        "regime": "broad_up",
        "label": "广泛上涨趋势",
        "direction_multipliers": {"LONG": 1.1, "SHORT": 0.65},
        "symbols": {
            "ALTUSDT": {
                "long_strength_percentile": 0.98,
                "short_strength_percentile": 0.02,
                "absolute_move_percentile": 0.80,
            }
        },
    }
    signal = {
        "signal": "LONG",
        "entry_type": "v3_breakout",
        "entry_type_label": "新趋势突破",
        "trend": True,
        "donchian_votes": 3,
        "donchian_models": 3,
        "multi_horizon_returns": [0.2, 0.5, 1.2, 3.0],
        "volume_acceleration": 2.5,
        "directed_trade_flow": 0.65,
        "expected_profit_pct": 1.2,
        "adverse_wick_ratio": 0.4,
        "impulse_atr": 1.0,
    }
    result = score_v3_opportunity(
        symbol="ALTUSDT",
        direction="LONG",
        signal=signal,
        ticker=_ticker("ALTUSDT", 8),
        market_context=context,
        medium_context={"ALTUSDT": {"trend_long": True, "path_efficiency": 0.42}},
        depth={"spread_pct": 0.02, "depth_notional": 20_000, "available": True},
        derivatives={"enabled": True, "confirmed": True, "score_delta": 10},
        event=None,
        cost_pct=0.12,
        config={},
    )

    assert result["passed"] is True
    assert result["tier"] == "A+"
    assert result["canary_eligible"] is True
    assert result["cost_ratio"] >= 3


def test_v3_a_plus_is_capped_without_medium_confirmation():
    context = {
        "regime": "broad_up",
        "label": "广泛上涨趋势",
        "direction_multipliers": {"LONG": 1.1, "SHORT": 0.65},
        "symbols": {"ALTUSDT": {"long_strength_percentile": 0.99, "absolute_move_percentile": 0.8}},
    }
    signal = {
        "signal": "LONG",
        "entry_type": "v3_breakout",
        "trend": True,
        "donchian_votes": 3,
        "donchian_models": 3,
        "multi_horizon_returns": [0.2, 0.5, 1.2, 3.0],
        "volume_acceleration": 2.5,
        "directed_trade_flow": 0.65,
        "expected_profit_pct": 1.2,
        "impulse_atr": 1.0,
    }
    result = score_v3_opportunity(
        symbol="ALTUSDT",
        direction="LONG",
        signal=signal,
        ticker=_ticker("ALTUSDT", 8),
        market_context=context,
        depth={"spread_pct": 0.02, "depth_notional": 20_000, "available": True},
        derivatives={"enabled": True, "confirmed": True},
        event=None,
        cost_pct=0.12,
        config={},
    )

    assert result["passed"] is True
    assert result["tier"] == "A"


def test_v3_blocks_countertrend_and_overextended_entries():
    context = {
        "regime": "broad_up",
        "label": "广泛上涨趋势",
        "direction_multipliers": {"LONG": 1.1, "SHORT": 0.65},
        "symbols": {"ALTUSDT": {"short_strength_percentile": 0.99, "absolute_move_percentile": 0.8}},
    }
    signal = {
        "signal": "SHORT",
        "entry_type": "v3_breakout",
        "trend": True,
        "donchian_votes": 3,
        "donchian_models": 3,
        "multi_horizon_returns": [0.2, 0.5, 1.2, 3.0],
        "volume_acceleration": 2.5,
        "directed_trade_flow": 0.65,
        "expected_profit_pct": 1.2,
        "impulse_atr": 1.5,
        "breakout_extension_atr": 0.8,
    }
    result = score_v3_opportunity(
        symbol="ALTUSDT",
        direction="SHORT",
        signal=signal,
        ticker=_ticker("ALTUSDT", -8),
        market_context=context,
        medium_context={"ALTUSDT": {"trend_short": True, "path_efficiency": 0.4}},
        depth={"spread_pct": 0.02, "depth_notional": 20_000, "available": True},
        derivatives={"enabled": True, "confirmed": True},
        event=None,
        cost_pct=0.12,
        config={},
    )

    assert result["passed"] is False
    assert result["countertrend_blocked"] is True
    assert result["overextended"] is True


def test_v3_live_candidate_requires_confirmed_orderbook_liquidity():
    context = {
        "regime": "broad_up",
        "label": "broad up",
        "direction_multipliers": {"LONG": 1.1, "SHORT": 0.65},
        "symbols": {
            "ALTUSDT": {
                "long_strength_percentile": 0.99,
                "short_strength_percentile": 0.01,
                "absolute_move_percentile": 0.9,
            }
        },
    }
    signal = {
        "signal": "LONG",
        "entry_type": "v3_breakout",
        "entry_type_label": "breakout",
        "trend": True,
        "donchian_votes": 3,
        "donchian_models": 3,
        "multi_horizon_returns": [0.2, 0.5, 1.2, 3.0],
        "volume_acceleration": 2.5,
        "directed_trade_flow": 0.65,
        "expected_profit_pct": 1.2,
        "adverse_wick_ratio": 0.4,
        "impulse_atr": 1.0,
    }

    result = score_v3_opportunity(
        symbol="ALTUSDT",
        direction="LONG",
        signal=signal,
        ticker=_ticker("ALTUSDT", 8),
        market_context=context,
        depth={"reason": "depth_not_checked", "available": False},
        derivatives={"enabled": True, "confirmed": True, "score_delta": 10},
        event=None,
        cost_pct=0.12,
        config={},
    )

    assert result["liquidity_safe"] is False
    assert result["eligible"] is False
    assert result["passed"] is False
    assert result["canary_eligible"] is False


def test_v3_b_tier_is_shadow_only_by_default():
    context = {
        "regime": "mixed",
        "label": "混合震荡",
        "direction_multipliers": {"LONG": 0.85, "SHORT": 0.85},
        "symbols": {
            "ALTUSDT": {
                "long_strength_percentile": 0.62,
                "short_strength_percentile": 0.38,
                "absolute_move_percentile": 0.6,
            }
        },
    }
    signal = {
        "signal": "LONG",
        "entry_type": "v3_prebreakout",
        "entry_type_label": "突破前抢跑",
        "trend": True,
        "donchian_votes": 0,
        "donchian_models": 3,
        "multi_horizon_returns": [0.1, 0.2, 0.3, 0.4],
        "volume_acceleration": 1.0,
        "directed_trade_flow": 0.52,
        "expected_profit_pct": 0.5,
        "adverse_wick_ratio": 0.2,
        "impulse_atr": 0.4,
    }
    result = score_v3_opportunity(
        symbol="ALTUSDT",
        direction="LONG",
        signal=signal,
        ticker=_ticker("ALTUSDT", 2),
        market_context=context,
        depth={"spread_pct": 0.04, "depth_notional": 8_000, "available": True},
        derivatives={"enabled": False},
        event=None,
        cost_pct=0.12,
        config={"opportunity_v3_b_score": 50.0},
    )

    assert result["tier"] == "B"
    assert result["eligible"] is True
    assert result["passed"] is False
    assert "影子" in result["blockers"][-1]


def test_countertrend_short_receives_regime_penalty_and_smaller_multiplier():
    context = {
        "regime": "broad_up",
        "label": "广泛上涨趋势",
        "direction_multipliers": {"LONG": 1.1, "SHORT": 0.65},
        "symbols": {
            "ALTUSDT": {
                "long_strength_percentile": 0.2,
                "short_strength_percentile": 0.8,
                "absolute_move_percentile": 0.8,
            }
        },
    }
    signal = {
        "signal": "SHORT",
        "entry_type": "v3_breakout",
        "entry_type_label": "新趋势突破",
        "trend": True,
        "donchian_votes": 2,
        "donchian_models": 3,
        "multi_horizon_returns": [0.2, 0.3, 0.5, 1.0],
        "volume_acceleration": 1.8,
        "directed_trade_flow": 0.6,
        "expected_profit_pct": 1.0,
        "adverse_wick_ratio": 0.4,
        "impulse_atr": 1.0,
    }
    result = score_v3_opportunity(
        symbol="ALTUSDT",
        direction="SHORT",
        signal=signal,
        ticker=_ticker("ALTUSDT", -5),
        market_context=context,
        depth={"spread_pct": 0.03, "depth_notional": 12_000, "available": True},
        derivatives={"enabled": False},
        event=None,
        cost_pct=0.12,
        config={},
    )

    assert result["penalties"]["countertrend_short"] == 15
    assert result["direction_multiplier"] == 0.65


def test_scanner_routes_extreme_mode_through_v3_without_window_backtest(monkeypatch):
    class Client:
        def ticker_24h(self, symbols=None):
            return [_ticker("ALTUSDT", 8)]

        def premium_index(self, symbols=None):
            return [{"symbol": "ALTUSDT", "lastFundingRate": "0", "markPrice": "1", "indexPrice": "1"}]

        def klines_history(self, symbol, interval, days):
            return _trend_bars()

        def depth(self, symbol, limit=5):
            return {"bids": [["0.999", "10000"]], "asks": [["1.001", "10000"]]}

        def open_interest_hist(self, symbol, period="5m", limit=12):
            return [{"sumOpenInterest": "100"}, {"sumOpenInterest": "105"}]

    monkeypatch.setattr(scanner, "discover_coin_symbols", lambda client, config: ["ALTUSDT"])
    monkeypatch.setattr(scanner, "apply_live_credit_to_candidate", lambda candidate, config: candidate)
    monkeypatch.setattr(scanner, "apply_live_reaction_to_candidate", lambda candidate, config: candidate)
    monkeypatch.setattr(scanner, "apply_strategy_evidence_to_candidate", lambda candidate, config: candidate)
    result = scanner.scan_growth_candidates(
        Client(),
        {
            "growth_mode": "extreme_sprint",
            "auto_risk_by_equity": False,
            "extreme_sprint_enabled": True,
            "extreme_sprint_confirmation": "ENABLE_EXTREME_SPRINT",
            "opportunity_v3_enabled": True,
            "opportunity_v3_long_strength_floor": 0.5,
            "opportunity_v3_a_score": 60,
            "opportunity_v3_a_plus_score": 75,
            "opportunity_v3_a_plus_min_cost_ratio": 2,
            "extreme_sprint_interval": "5m",
            "extreme_sprint_recent_days": 2,
            "extreme_sprint_risk_per_trade_pct": 10,
            "extreme_sprint_max_leverage": 5,
            "extreme_sprint_max_symbol_margin_pct": 90,
            "allow_short": False,
            "quality_backtest_days": [2],
            "min_order_filter_enabled": False,
            "market_stream_dynamic_enabled": False,
            "shadow_trading_enabled": False,
        },
        {"equity": 30},
    )

    candidate = result["candidates"][0]
    assert candidate["strategy_family"] == "extreme_v3_roll"
    assert candidate["strategy_generation"] == "v3"
    assert candidate["backtests"][2]["diagnostic"] == "offline_v3_validation"
    assert result["funnel"]["opportunity_v3"]["enabled"] is True


def test_scanner_v3_uses_stream_depth_without_rest_depth_call(monkeypatch):
    class Client:
        def ticker_24h(self, symbols=None):
            return [_ticker("ALTUSDT", 8)]

        def premium_index(self, symbols=None):
            return [{"symbol": "ALTUSDT", "lastFundingRate": "0", "markPrice": "1", "indexPrice": "1"}]

        def klines_history(self, symbol, interval, days):
            return _trend_bars()

        def depth(self, symbol, limit=5):
            raise AssertionError("REST depth must not be called when a fresh stream snapshot exists")

        def open_interest_hist(self, symbol, period="5m", limit=12):
            return [{"sumOpenInterest": "100"}, {"sumOpenInterest": "105"}]

    monkeypatch.setattr(scanner, "discover_coin_symbols", lambda client, config: ["ALTUSDT"])
    monkeypatch.setattr(
        scanner,
        "stream_depth",
        lambda symbol, max_age_seconds=8: {
            "available": True,
            "bids": [["0.9998", "10000"]],
            "asks": [["1.0002", "10000"]],
        },
    )
    monkeypatch.setattr(scanner, "apply_live_credit_to_candidate", lambda candidate, config: candidate)
    monkeypatch.setattr(scanner, "apply_live_reaction_to_candidate", lambda candidate, config: candidate)
    monkeypatch.setattr(scanner, "apply_strategy_evidence_to_candidate", lambda candidate, config: candidate)

    result = scanner.scan_growth_candidates(
        Client(),
        {
            "growth_mode": "extreme_sprint",
            "auto_risk_by_equity": False,
            "extreme_sprint_enabled": True,
            "extreme_sprint_confirmation": "ENABLE_EXTREME_SPRINT",
            "opportunity_v3_enabled": True,
            "opportunity_v3_long_strength_floor": 0.5,
            "opportunity_v3_a_score": 60,
            "opportunity_v3_a_plus_score": 75,
            "opportunity_v3_a_plus_min_cost_ratio": 2,
            "extreme_sprint_interval": "5m",
            "extreme_sprint_recent_days": 2,
            "extreme_sprint_risk_per_trade_pct": 10,
            "extreme_sprint_max_leverage": 5,
            "extreme_sprint_max_symbol_margin_pct": 90,
            "allow_short": False,
            "quality_backtest_days": [2],
            "min_order_filter_enabled": False,
            "market_stream_dynamic_enabled": False,
            "shadow_trading_enabled": False,
        },
        {"equity": 30},
    )

    assert result["candidates"][0]["depth_checked"] is True
    assert result["candidates"][0]["opportunity_v3"]["liquidity_safe"] is True


def test_v31_challenger_requires_aligned_medium_horizon():
    medium = build_v31_medium_context({"ALTUSDT": _trend_bars(200)})
    signal = {
        "signal": "LONG",
        "entry_type": "v3_breakout",
        "protection_profile": {"stop_atr": 0.9, "take_profit_atr": 2.2, "max_hold_bars": 18},
    }
    challenger = build_v31_challenger(
        symbol="ALTUSDT",
        direction="LONG",
        signal=signal,
        opportunity={"score": 82.0, "liquidity_safe": True},
        medium_context=medium,
        config={"opportunity_v31_shadow_min_score": 60.0},
    )

    assert challenger["enabled"] is True
    assert challenger["eligible"] is True
    assert challenger["shadow_only"] is True
    assert challenger["protection_profile"]["take_profit_atr"] == 2.8
    assert challenger["protection_profile"]["protection_version"] == "v5_dynamic"
