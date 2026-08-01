from datetime import datetime, timedelta, timezone

from app.adaptive_30d_momentum_shadow import (
    STRATEGY_FAMILY,
    STRATEGY_VERSION,
    build_adaptive_30d_shadow_candidate,
    completed_hourly_metrics,
)


def _bars(return_pct: float, boundary: datetime) -> list[list[object]]:
    start = boundary - timedelta(hours=721)
    rows = []
    for index in range(722):
        progress = index / 720
        close = 100 * (1 + return_pct * min(progress, 1))
        open_ms = int((start + timedelta(hours=index)).timestamp() * 1000)
        rows.append(
            [
                open_ms,
                str(close),
                str(close + 1),
                str(close - 1),
                str(close),
                "100",
                open_ms + 3_600_000 - 1,
                "10000",
                100,
                "50",
                "5000",
                "0",
            ]
        )
    return rows


def _ticker(now: datetime, price: float = 100.0, volume: float = 50_000_000) -> dict:
    return {
        "lastPrice": str(price),
        "quoteVolume": str(volume),
        "updated_at": now.isoformat(),
    }


class FakeClient:
    def __init__(self, now: datetime, down: bool = False):
        self.now = now
        self.down = down

    def exchange_info(self) -> dict:
        onboard = int((self.now - timedelta(days=100)).timestamp() * 1000)
        symbols = [
            {
                "symbol": f"ALT{index}USDT",
                "status": "TRADING",
                "contractType": "PERPETUAL",
                "quoteAsset": "USDT",
                "onboardDate": onboard,
            }
            for index in range(70)
        ]
        return {"symbols": symbols}

    def klines(self, symbol: str, interval: str, limit: int) -> list[list[object]]:
        assert interval == "1h"
        assert limit == 722
        if symbol == "BTCUSDT":
            value = -0.05 if self.down else 0.05
        else:
            index = int(symbol.removeprefix("ALT").removesuffix("USDT"))
            value = (-0.03 - index / 10_000) if self.down else (0.03 + index / 10_000)
        return _bars(value, self.now.replace(minute=0, second=0, microsecond=0))


def _snapshot(now: datetime) -> dict:
    return {
        "tickers": {
            **{"BTCUSDT": _ticker(now, 50_000)},
            **{f"ALT{index}USDT": _ticker(now, 100 + index) for index in range(70)},
        }
    }


def test_completed_metrics_require_continuous_thirty_days():
    boundary = datetime(2026, 8, 1, tzinfo=timezone.utc)
    rows = _bars(0.05, boundary)
    value = completed_hourly_metrics(rows, int(boundary.timestamp() * 1000))
    assert value is not None
    assert 0.049 < value["return_30d"] < 0.051

    rows.pop(200)
    assert completed_hourly_metrics(rows, int(boundary.timestamp() * 1000)) is None


def test_builds_long_independent_future_shadow():
    now = datetime(2026, 8, 1, 0, 5, tzinfo=timezone.utc)
    candidate, status = build_adaptive_30d_shadow_candidate(
        FakeClient(now), _snapshot(now), {}, now, sleep_fn=lambda _: None
    )

    assert status["status"] == "candidate_ready"
    assert candidate is not None
    assert candidate["strategy_family"] == STRATEGY_FAMILY
    assert candidate["strategy_version"] == STRATEGY_VERSION
    assert candidate["direction"] == "LONG"
    assert candidate["symbol"] == "ALT69USDT"
    assert candidate["shadow_force_eligible"] is True
    assert candidate["shadow_single_position"] is True
    assert candidate["passed"] is False
    assert candidate["signal"]["stop"] < candidate["signal"]["last_price"]
    assert candidate["signal"]["take_profit"] > candidate["signal"]["last_price"]
    assert candidate["research_context"]["gate_policy"] == "ungated_future_paper_all_candidates"


def test_builds_short_when_btc_and_breadth_are_negative():
    now = datetime(2026, 8, 1, 0, 5, tzinfo=timezone.utc)
    candidate, status = build_adaptive_30d_shadow_candidate(
        FakeClient(now, down=True), _snapshot(now), {}, now, sleep_fn=lambda _: None
    )

    assert status["status"] == "candidate_ready"
    assert candidate is not None
    assert candidate["direction"] == "SHORT"
    assert candidate["symbol"] == "ALT69USDT"
    assert candidate["signal"]["stop"] > candidate["signal"]["last_price"]
    assert candidate["signal"]["take_profit"] < candidate["signal"]["last_price"]


def test_skips_outside_fixed_daily_window():
    now = datetime(2026, 8, 1, 1, 5, tzinfo=timezone.utc)
    candidate, status = build_adaptive_30d_shadow_candidate(
        FakeClient(now), _snapshot(now), {}, now, sleep_fn=lambda _: None
    )
    assert candidate is None
    assert status["status"] == "outside_daily_window"
