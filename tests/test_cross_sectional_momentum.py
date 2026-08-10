from datetime import datetime, timedelta, timezone

from app.cross_sectional_momentum import (
    STRATEGY_FAMILY,
    STRATEGY_VERSION,
    build_cross_sectional_shadow_candidate,
    select_cross_sectional_signal,
)


def _ticker(now: datetime, change: float, volume: float = 10_000_000, price: float = 1.0) -> dict:
    return {
        "lastPrice": str(price),
        "priceChangePercent": str(change),
        "quoteVolume": str(volume),
        "updated_at": now.isoformat(),
    }


def _snapshot(now: datetime, btc_change: float = 2.0) -> dict:
    tickers = {"BTCUSDT": _ticker(now, btc_change, price=50_000)}
    for index in range(70):
        tickers[f"ALT{index}USDT"] = _ticker(now, -2 + index * 0.1, price=1 + index / 10)
    return {"tickers": tickers}


def _bars() -> list[list[object]]:
    start = 1_700_000_000_000
    rows = []
    for index in range(72):
        close = 100 + index * 0.2
        rows.append(
            [
                start + index * 3_600_000,
                str(close - 0.1),
                str(close + 1.0),
                str(close - 1.0),
                str(close),
                "100",
                start + (index + 1) * 3_600_000 - 1,
                "10000",
                100,
                "50",
                "5000",
                "0",
            ]
        )
    return rows


class FakeClient:
    def exchange_info(self) -> dict:
        return {
            "symbols": [
                {
                    "symbol": f"ALT{index}USDT",
                    "onboardDate": 1_700_000_000_000,
                }
                for index in range(70)
            ]
        }

    def klines(self, symbol: str, interval: str, limit: int) -> list[list[object]]:
        assert interval == "1h"
        assert limit == 72
        return _bars()


def test_selects_strongest_long_when_btc_and_breadth_are_positive():
    now = datetime(2026, 7, 31, 8, 1, 30, tzinfo=timezone.utc)
    snapshot = _snapshot(now)
    for ticker in snapshot["tickers"].values():
        if ticker is not snapshot["tickers"]["BTCUSDT"]:
            ticker["priceChangePercent"] = str(float(ticker["priceChangePercent"]) + 3)

    selected = select_cross_sectional_signal(snapshot, {}, now)

    assert selected["status"] == "selected"
    assert selected["direction"] == "LONG"
    assert selected["symbol"] == "ALT69USDT"
    assert selected["universe_size"] == 70
    assert selected["execution_delay_seconds"] == 90


def test_skips_mixed_btc_and_altcoin_breadth():
    now = datetime(2026, 7, 31, 8, 1, 30, tzinfo=timezone.utc)
    snapshot = _snapshot(now, btc_change=-1.0)
    for ticker in snapshot["tickers"].values():
        if ticker is not snapshot["tickers"]["BTCUSDT"]:
            ticker["priceChangePercent"] = str(abs(float(ticker["priceChangePercent"])) + 1)

    selected = select_cross_sectional_signal(snapshot, {}, now)

    assert selected["status"] == "mixed_market"
    assert selected["btc_momentum_24h_pct"] < 0
    assert selected["breadth_median_24h_pct"] > 0


def test_rejects_stale_or_late_snapshot():
    now = datetime(2026, 7, 31, 8, 4, 0, tzinfo=timezone.utc)
    late = select_cross_sectional_signal(_snapshot(now), {}, now)
    assert late["status"] == "outside_entry_window"

    in_window = now.replace(minute=1, second=30)
    stale_snapshot = _snapshot(in_window - timedelta(minutes=1))
    stale = select_cross_sectional_signal(stale_snapshot, {}, in_window)
    assert stale["status"] == "insufficient_universe"


def test_builds_isolated_single_position_shadow_with_capped_stop():
    now = datetime(2026, 7, 31, 8, 1, 30, tzinfo=timezone.utc)
    snapshot = _snapshot(now)
    for ticker in snapshot["tickers"].values():
        if ticker is not snapshot["tickers"]["BTCUSDT"]:
            ticker["priceChangePercent"] = str(float(ticker["priceChangePercent"]) + 3)

    candidate, status = build_cross_sectional_shadow_candidate(FakeClient(), snapshot, {}, now)

    assert status["status"] == "candidate_ready"
    assert candidate is not None
    assert candidate["strategy_family"] == STRATEGY_FAMILY
    assert candidate["strategy_version"] == STRATEGY_VERSION
    assert candidate["shadow_force_eligible"] is True
    assert candidate["shadow_single_position"] is True
    assert candidate["passed"] is False
    assert candidate["signal"]["stop"] < candidate["signal"]["last_price"]
    assert candidate["signal"]["take_profit"] > candidate["signal"]["last_price"]
    assert candidate["signal"]["protection_profile"]["max_hold_seconds"] == 12 * 3600
    assert candidate["research_context"]["symbol_age_days"] > 30
    assert candidate["shadow_dedupe_key"] == candidate["opportunity_id"]
    assert candidate["research_context"]["episode_minutes"] == 360


def test_shadow_candidate_reuses_one_id_within_an_independent_episode():
    now = datetime(2026, 7, 31, 8, 1, 30, tzinfo=timezone.utc)
    snapshot = _snapshot(now)
    for ticker in snapshot["tickers"].values():
        if ticker is not snapshot["tickers"]["BTCUSDT"]:
            ticker["priceChangePercent"] = str(float(ticker["priceChangePercent"]) + 3)

    first, _ = build_cross_sectional_shadow_candidate(FakeClient(), snapshot, {}, now)
    later = now + timedelta(minutes=1)
    later_snapshot = _snapshot(later)
    for ticker in later_snapshot["tickers"].values():
        if ticker is not later_snapshot["tickers"]["BTCUSDT"]:
            ticker["priceChangePercent"] = str(float(ticker["priceChangePercent"]) + 3)
    second, _ = build_cross_sectional_shadow_candidate(FakeClient(), later_snapshot, {}, later)

    assert first is not None and second is not None
    assert first["opportunity_id"] == second["opportunity_id"]
    assert first["shadow_dedupe_key"] == second["shadow_dedupe_key"]


def test_skips_selected_symbol_that_is_too_new_without_substitution():
    now = datetime(2026, 7, 31, 8, 1, 30, tzinfo=timezone.utc)
    snapshot = _snapshot(now)
    for ticker in snapshot["tickers"].values():
        if ticker is not snapshot["tickers"]["BTCUSDT"]:
            ticker["priceChangePercent"] = str(float(ticker["priceChangePercent"]) + 3)

    class NewListingClient(FakeClient):
        def exchange_info(self) -> dict:
            payload = super().exchange_info()
            for item in payload["symbols"]:
                if item["symbol"] == "ALT69USDT":
                    item["onboardDate"] = int(
                        (now - timedelta(days=5)).timestamp() * 1000
                    )
            return payload

    candidate, status = build_cross_sectional_shadow_candidate(
        NewListingClient(),
        snapshot,
        {"xmom_shadow_min_onboard_age_days": 30},
        now,
    )

    assert candidate is None
    assert status["status"] == "listing_too_new"
    assert status["symbol"] == "ALT69USDT"
    assert status["symbol_age_days"] == 5.0
