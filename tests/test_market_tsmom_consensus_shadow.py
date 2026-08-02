from datetime import datetime, timedelta, timezone

from app.market_tsmom_consensus_shadow import (
    STRATEGY_FAMILY,
    STRATEGY_VERSION,
    build_market_tsmom_shadow_candidate,
    completed_daily_series,
    market_consensus_metrics,
)
from app.models import TradingConfig
from app.shadow_trading import update_shadow_trades
from app.telemetry import connect


def _bars(daily_return: float, boundary: datetime) -> list[list[object]]:
    start = boundary - timedelta(days=60)
    close = 100.0
    rows = []
    for index in range(60):
        close *= 1.0 + daily_return
        open_ms = int((start + timedelta(days=index)).timestamp() * 1000)
        rows.append(
            [
                open_ms,
                str(close),
                str(close),
                str(close),
                str(close),
                "100",
                open_ms + 86_400_000 - 1,
                str(1_000_000 + index),
                100,
                "50",
                "500000",
                "0",
            ]
        )
    return rows


def _ticker(now: datetime, price: float = 100.0) -> dict:
    return {
        "lastPrice": str(price),
        "quoteVolume": "50000000",
        "updated_at": now.isoformat(),
    }


class FakeClient:
    def __init__(self, now: datetime, daily_return: float = 0.01):
        self.now = now
        self.daily_return = daily_return

    def exchange_info(self) -> dict:
        onboard = int((self.now - timedelta(days=365)).timestamp() * 1000)
        return {
            "symbols": [
                {
                    "symbol": f"ALT{index}USDT",
                    "status": "TRADING",
                    "contractType": "PERPETUAL",
                    "quoteAsset": "USDT",
                    "onboardDate": onboard,
                }
                for index in range(30)
            ]
        }

    def klines(self, symbol: str, interval: str, limit: int) -> list[list[object]]:
        assert interval == "1d"
        assert limit == 60
        return _bars(self.daily_return, self.now.replace(hour=0, minute=0, second=0, microsecond=0))


def _snapshot(now: datetime) -> dict:
    return {
        "tickers": {
            "BTCUSDT": _ticker(now, 50_000),
            **{f"ALT{index}USDT": _ticker(now, 100 + index) for index in range(30)},
        }
    }


def test_daily_series_requires_continuity() -> None:
    boundary = datetime(2026, 8, 1, tzinfo=timezone.utc)
    rows = _bars(0.01, boundary)
    value = completed_daily_series(rows, int(boundary.timestamp() * 1000))
    assert value is not None
    assert len(value["returns"]) == 56
    rows.pop(20)
    assert completed_daily_series(rows, int(boundary.timestamp() * 1000)) is None


def test_market_consensus_compounds_equal_weight_returns() -> None:
    series = {
        f"ALT{index}USDT": {
            "returns": [0.01] * 56,
            "median_quote_volume_30d": 1_000_000 + index,
        }
        for index in range(20)
    }
    result = market_consensus_metrics(series)
    assert result is not None
    assert result["momentum_28d"] > 0.30
    assert result["momentum_56d"] > 0.70


def test_builds_isolated_btc_long_shadow() -> None:
    now = datetime(2026, 8, 1, 0, 5, tzinfo=timezone.utc)
    candidate, status = build_market_tsmom_shadow_candidate(
        FakeClient(now), _snapshot(now), {}, now, sleep_fn=lambda _: None
    )

    assert status["status"] == "candidate_ready"
    assert candidate is not None
    assert candidate["strategy_family"] == STRATEGY_FAMILY
    assert candidate["strategy_version"] == STRATEGY_VERSION
    assert candidate["symbol"] == "BTCUSDT"
    assert candidate["direction"] == "LONG"
    assert candidate["passed"] is False
    assert candidate["evidence_type"] == "independent_realtime"
    assert candidate["shadow_disable_take_profit"] is True
    assert candidate["research_context"]["reference_risk_pct"] == 15.0
    assert candidate["research_context"]["max_risk_cap_pct"] == 30.0


def test_no_candidate_when_fast_momentum_is_below_threshold() -> None:
    now = datetime(2026, 8, 1, 0, 5, tzinfo=timezone.utc)
    candidate, status = build_market_tsmom_shadow_candidate(
        FakeClient(now, daily_return=0.001), _snapshot(now), {}, now, sleep_fn=lambda _: None
    )

    assert candidate is None
    assert status["status"] == "no_signal"


def test_candidate_opens_only_as_isolated_shadow_without_fixed_take_profit(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    now = datetime(2026, 8, 1, 0, 5, tzinfo=timezone.utc)
    candidate, _ = build_market_tsmom_shadow_candidate(
        FakeClient(now), _snapshot(now), {}, now, sleep_fn=lambda _: None
    )
    assert candidate is not None
    result = update_shadow_trades(
        [candidate],
        {
            "shadow_trading_enabled": True,
            "shadow_reference_notional_usdt": 20,
            "shadow_round_trip_cost_pct": 0.12,
        },
    )

    assert result["opened"] == 1
    with connect() as conn:
        row = conn.execute(
            "SELECT evidence_type, payload FROM shadow_trades"
        ).fetchone()
    assert row[0] == "independent_realtime"
    assert '"shadow_disable_take_profit":true' in row[1]

    price_update = {
        **candidate,
        "ticker": {"last": candidate["signal"]["take_profit"] * 2},
        "signal": {
            **candidate["signal"],
            "last_price": candidate["signal"]["take_profit"] * 2,
        },
    }
    settled = update_shadow_trades(
        [price_update],
        {
            "shadow_trading_enabled": True,
            "shadow_reference_notional_usdt": 20,
            "shadow_round_trip_cost_pct": 0.12,
        },
    )
    assert settled["closed"] == 0


def test_market_tsmom_settings_are_accepted_by_app_config() -> None:
    config = TradingConfig(
        market_tsmom_shadow_enabled=False,
        market_tsmom_shadow_prefetch_symbols=60,
        market_tsmom_shadow_top_third_threshold_pct=11.25,
        market_tsmom_shadow_reference_risk_pct=12.0,
    )

    assert config.market_tsmom_shadow_enabled is False
    assert config.market_tsmom_shadow_prefetch_symbols == 60
    assert config.market_tsmom_shadow_top_third_threshold_pct == 11.25
    assert config.market_tsmom_shadow_reference_risk_pct == 12.0
