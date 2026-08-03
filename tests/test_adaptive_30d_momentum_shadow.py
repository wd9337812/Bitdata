from datetime import datetime, timedelta, timezone

from app.adaptive_30d_momentum_shadow import (
    LIVE_STRATEGY_VERSION,
    STRATEGY_FAMILY,
    STRATEGY_VERSION,
    adaptive_30d_direction_gate,
    build_adaptive_30d_live_decision,
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


def test_direction_gate_uses_audited_seed_and_blocks_weak_short_side():
    long_gate = adaptive_30d_direction_gate("LONG", {})
    short_gate = adaptive_30d_direction_gate("SHORT", {})

    assert long_gate["allowed"] is True
    assert long_gate["profit_factor"] > 2.4
    assert short_gate["allowed"] is False
    assert short_gate["profit_factor"] < 1.25


def test_live_decision_sizes_long_with_hard_stop_headroom():
    now = datetime(2026, 8, 1, 0, 5, tzinfo=timezone.utc)
    candidate, _ = build_adaptive_30d_shadow_candidate(
        FakeClient(now), _snapshot(now), {}, now, sleep_fn=lambda _: None
    )
    decision = build_adaptive_30d_live_decision(
        {},
        {},
        {"equity": 15.0, "available_balance": 15.0, "positions": []},
        now,
        candidate=candidate,
        closed_net_pcts=[10, 10, 10],
    )

    assert decision["action"] == "OPEN_LONG"
    assert decision["strategy_version"] == LIVE_STRATEGY_VERSION
    assert 0 < decision["risk_pct"] <= 15.0
    assert decision["estimated_notional"] >= 10.0
    assert decision["candidate"]["direction_gate"]["allowed"] is True
    assert decision["signal"]["stop"] < decision["signal"]["last_price"]
    profile = decision["signal"]["protection_profile"]
    assert profile["protection_version"] == "adaptive_30d_daily_v1"
    assert profile["runtime_intraday_trailing_enabled"] is False


def test_live_decision_supports_contract_safe_short():
    now = datetime(2026, 8, 1, 0, 5, tzinfo=timezone.utc)
    candidate, _ = build_adaptive_30d_shadow_candidate(
        FakeClient(now, down=True), _snapshot(now), {}, now, sleep_fn=lambda _: None
    )
    decision = build_adaptive_30d_live_decision(
        {},
        {},
        {"equity": 15.0, "available_balance": 15.0, "positions": []},
        now,
        candidate=candidate,
        closed_net_pcts=[20, 20, 20],
    )

    assert decision["action"] == "OPEN_SHORT"
    assert decision["quantity"] > 0
    assert decision["signal"]["stop"] > decision["signal"]["last_price"]


def test_live_decision_refuses_equity_without_hard_stop_reserve():
    now = datetime(2026, 8, 1, 0, 5, tzinfo=timezone.utc)
    candidate, _ = build_adaptive_30d_shadow_candidate(
        FakeClient(now), _snapshot(now), {}, now, sleep_fn=lambda _: None
    )
    decision = build_adaptive_30d_live_decision(
        {},
        {},
        {"equity": 5.4, "available_balance": 5.4, "positions": []},
        now,
        candidate=candidate,
        closed_net_pcts=[20, 20, 20],
    )

    assert decision["action"] == "WAIT"
    assert decision["reason"] == "adaptive_30d_insufficient_hard_stop_headroom"
