from datetime import datetime, timedelta, timezone

import app.market_tsmom_consensus_shadow as market_tsmom_module
import app.runner as runner_module
from app.market_tsmom_consensus_shadow import (
    STRATEGY_FAMILY,
    STRATEGY_VERSION,
    build_market_tsmom_live_decision,
    build_market_tsmom_shadow_candidate,
    completed_daily_series,
    market_consensus_metrics,
    status_has_current_day_evaluation,
)
from app.models import TradingConfig
from app.shadow_trading import manage_shadow_strategy_positions, update_shadow_trades
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
            "ETHUSDT": _ticker(now, 2_000),
            "BNBUSDT": _ticker(now, 600),
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


def test_builds_contract_executable_long_shadow() -> None:
    now = datetime(2026, 8, 1, 0, 5, tzinfo=timezone.utc)
    candidate, status = build_market_tsmom_shadow_candidate(
        FakeClient(now), _snapshot(now), {}, now, sleep_fn=lambda _: None
    )

    assert status["status"] == "candidate_ready"
    assert candidate is not None
    assert candidate["strategy_family"] == STRATEGY_FAMILY
    assert candidate["strategy_version"] == STRATEGY_VERSION
    assert candidate["symbol"] == "BNBUSDT"
    assert set(candidate["execution_options"]) == {"BNBUSDT", "BTCUSDT", "ETHUSDT"}
    assert candidate["reference_execution_attempts"]["BTCUSDT"]["eligible"] is False
    assert candidate["reference_execution_attempts"]["BNBUSDT"]["eligible"] is True
    assert candidate["direction"] == "LONG"
    assert candidate["passed"] is False
    assert candidate["evidence_type"] == "independent_realtime"
    assert candidate["shadow_disable_take_profit"] is True
    assert candidate["research_context"]["reference_risk_pct"] == 10.0
    assert candidate["research_context"]["max_risk_cap_pct"] == 30.0
    assert candidate["signal"]["protection_profile"]["stop_pct"] == 10.0
    assert candidate["signal"]["protection_profile"]["daily_stop_audit_enabled"] is False
    assert candidate["shadow_max_hold_minutes"] == 5 * 24 * 60


def test_frequency_challenger_reuses_entry_and_isolated_version() -> None:
    now = datetime(2026, 8, 1, 0, 5, tzinfo=timezone.utc)
    active, _ = build_market_tsmom_shadow_candidate(
        FakeClient(now), _snapshot(now), {}, now, sleep_fn=lambda _: None
    )
    assert active is not None

    challenger = market_tsmom_module.build_frequency_challenger_candidate(active, {})

    assert challenger is not None
    assert challenger["symbol"] == active["symbol"] == "BNBUSDT"
    assert challenger["signal"]["last_price"] == active["signal"]["last_price"]
    assert challenger["strategy_version"] == market_tsmom_module.FREQUENCY_CHALLENGER_VERSION
    assert challenger["strategy_version"] != active["strategy_version"]
    assert challenger["shadow_max_hold_minutes"] == 3 * 24 * 60
    assert challenger["signal"]["stop"] == challenger["signal"]["last_price"] * 0.85
    assert challenger["signal"]["protection_profile"]["daily_stop_audit_enabled"] is False
    assert challenger["research_context"]["gate_policy"] == "isolated_frequency_shadow_no_live_effect"


def test_frequency_challenger_can_be_disabled() -> None:
    assert market_tsmom_module.build_frequency_challenger_candidate(
        {"signal": {"last_price": 100.0}},
        {"market_tsmom_frequency_challenger_enabled": False},
    ) is None


def test_live_profile_and_frequency_challenger_open_as_separate_shadows(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    now = datetime(2026, 8, 1, 0, 5, tzinfo=timezone.utc)
    active, _ = build_market_tsmom_shadow_candidate(
        FakeClient(now), _snapshot(now), {}, now, sleep_fn=lambda _: None
    )
    assert active is not None
    challenger = market_tsmom_module.build_frequency_challenger_candidate(active, {})
    assert challenger is not None

    result = update_shadow_trades(
        [active, challenger],
        {
            "shadow_trading_enabled": True,
            "shadow_reference_notional_usdt": 20,
            "shadow_round_trip_cost_pct": 0.12,
        },
    )

    assert result["opened"] == 2
    with connect() as conn:
        rows = conn.execute(
            "SELECT strategy_version, expires_at, stop FROM shadow_trades ORDER BY strategy_version"
        ).fetchall()
    assert {row[0] for row in rows} == {
        STRATEGY_VERSION,
        market_tsmom_module.FREQUENCY_CHALLENGER_VERSION,
    }
    assert len({row[1] for row in rows}) == 2
    assert len({round(float(row[2]), 8) for row in rows}) == 2


def test_no_candidate_when_fast_momentum_is_below_threshold() -> None:
    now = datetime(2026, 8, 1, 0, 5, tzinfo=timezone.utc)
    candidate, status = build_market_tsmom_shadow_candidate(
        FakeClient(now, daily_return=0.001), _snapshot(now), {}, now, sleep_fn=lambda _: None
    )

    assert candidate is None
    assert status["status"] == "no_signal"


def test_new_release_can_bootstrap_after_daily_window() -> None:
    now = datetime(2026, 8, 1, 3, 5, tzinfo=timezone.utc)
    blocked, blocked_status = build_market_tsmom_shadow_candidate(
        FakeClient(now), _snapshot(now), {}, now, sleep_fn=lambda _: None
    )
    candidate, status = build_market_tsmom_shadow_candidate(
        FakeClient(now),
        _snapshot(now),
        {},
        now,
        sleep_fn=lambda _: None,
        allow_outside_window=True,
    )

    assert blocked is None
    assert blocked_status["status"] == "outside_daily_window"
    assert candidate is not None
    assert status["status"] == "candidate_ready"


def test_current_day_status_survives_same_version_restart() -> None:
    now = datetime(2026, 8, 1, 3, 5, tzinfo=timezone.utc)
    current = {
        "strategy_version": STRATEGY_VERSION,
        "status": "candidate_ready",
        "signal_boundary": "2026-08-01T00:00:00+00:00",
        "candidate": {"symbol": "ETHUSDT"},
    }

    assert status_has_current_day_evaluation(current, now) is True
    assert status_has_current_day_evaluation(
        {**current, "signal_boundary": "2026-07-31T00:00:00+00:00"}, now
    ) is False
    assert status_has_current_day_evaluation(
        {**current, "strategy_version": "old"}, now
    ) is False


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


def test_strategy_management_tightens_stop_and_closes_on_signal_off(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    now = datetime(2026, 8, 1, 0, 5, tzinfo=timezone.utc)
    candidate, _ = build_market_tsmom_shadow_candidate(
        FakeClient(now), _snapshot(now), {}, now, sleep_fn=lambda _: None
    )
    assert candidate is not None
    update_shadow_trades(
        [candidate],
        {
            "shadow_trading_enabled": True,
            "shadow_reference_notional_usdt": 20,
            "shadow_round_trip_cost_pct": 0.12,
        },
    )
    initial_stop = float(candidate["signal"]["stop"])
    tightened = manage_shadow_strategy_positions(
        STRATEGY_FAMILY,
        STRATEGY_VERSION,
        candidate["symbol"],
        trailing_stop=initial_stop * 1.01,
    )
    assert tightened == {"updated": 1, "closed": 0}
    ignored = manage_shadow_strategy_positions(
        STRATEGY_FAMILY,
        STRATEGY_VERSION,
        candidate["symbol"],
        trailing_stop=initial_stop * 0.99,
    )
    assert ignored == {"updated": 0, "closed": 0}
    closed = manage_shadow_strategy_positions(
        STRATEGY_FAMILY,
        STRATEGY_VERSION,
        candidate["symbol"],
        exit_price=50_500,
        outcome="MARKET_SIGNAL_OFF",
    )
    assert closed == {"updated": 0, "closed": 1}
    with connect() as conn:
        row = conn.execute(
            "SELECT status, outcome, net_pnl FROM shadow_trades"
        ).fetchone()
    assert row[0] == "CLOSED"
    assert row[1] == "MARKET_SIGNAL_OFF"
    assert float(row[2]) > 0


def test_market_tsmom_settings_are_accepted_by_app_config() -> None:
    config = TradingConfig(
        market_tsmom_shadow_enabled=False,
        market_tsmom_shadow_prefetch_symbols=60,
        market_tsmom_shadow_top_third_threshold_pct=11.25,
        market_tsmom_shadow_reference_risk_pct=12.0,
        market_tsmom_shadow_atr_multiple=2.5,
    )

    assert config.market_tsmom_shadow_enabled is False
    assert config.market_tsmom_shadow_prefetch_symbols == 60
    assert config.market_tsmom_shadow_top_third_threshold_pct == 11.25
    assert config.market_tsmom_shadow_reference_risk_pct == 12.0
    assert config.market_tsmom_shadow_atr_multiple == 2.5


def test_live_takeover_decision_requires_headroom_and_preserves_daily_profile(
    monkeypatch,
) -> None:
    now = datetime(2026, 8, 1, 0, 5, tzinfo=timezone.utc)
    candidate, _ = build_market_tsmom_shadow_candidate(
        FakeClient(now), _snapshot(now), {}, now, sleep_fn=lambda _: None
    )
    assert candidate is not None
    monkeypatch.setattr(
        market_tsmom_module,
        "current_market_tsmom_candidate",
        lambda _now=None: candidate,
    )
    blocked = build_market_tsmom_live_decision(
        {"hard_stop_equity": 5.0, "market_tsmom_live_min_equity_usdt": 10.0},
        {},
        {"equity": 5.4, "available_balance": 5.4, "positions": []},
        now,
    )
    assert blocked["action"] == "WAIT"
    assert blocked["reason"] == "market_tsmom_insufficient_hard_stop_headroom"

    decision = build_market_tsmom_live_decision(
        {
            "hard_stop_equity": 5.0,
            "market_tsmom_live_min_equity_usdt": 10.0,
            "market_tsmom_live_risk_pct": 10.0,
            "market_tsmom_live_leverage": 1,
            "market_tsmom_live_margin_pct": 90.0,
            "effective_min_order_notional_usdt": 10.0,
        },
        {},
        {"equity": 20.0, "available_balance": 20.0, "positions": []},
        now,
    )
    assert decision["action"] == "OPEN_LONG"
    assert decision["symbol"] == "BNBUSDT"
    assert decision["risk"]["execution_fallback_used"] is False
    assert decision["strategy_version"] == STRATEGY_VERSION
    assert decision["risk_pct"] <= 10.0
    assert decision["estimated_notional"] >= 10.0
    assert decision["signal"]["protection_profile"]["runtime_intraday_trailing_enabled"] is False
    assert decision["signal"]["protection_profile"]["protection_version"] == "market_tsmom_bnb_time5_v4"
    assert decision["signal"]["protection_profile"]["max_hold_seconds"] == 5 * 24 * 3600

    preferred = build_market_tsmom_live_decision(
        {
            "hard_stop_equity": 5.0,
            "market_tsmom_live_min_equity_usdt": 10.0,
            "market_tsmom_live_risk_pct": 10.0,
            "market_tsmom_live_leverage": 2,
            "market_tsmom_live_margin_pct": 90.0,
            "effective_min_order_notional_usdt": 10.0,
        },
        {},
        {"equity": 100.0, "available_balance": 100.0, "positions": []},
        now,
    )
    assert preferred["action"] == "OPEN_LONG"
    assert preferred["symbol"] == "BNBUSDT"
    assert preferred["risk"]["execution_fallback_used"] is False

    duplicate = build_market_tsmom_live_decision(
        {
            "hard_stop_equity": 5.0,
            "market_tsmom_live_min_equity_usdt": 10.0,
        },
        {"market_tsmom_live_entry_day": now.date().isoformat()},
        {"equity": 20.0, "available_balance": 20.0, "positions": []},
        now,
    )
    assert duplicate["action"] == "WAIT"
    assert duplicate["reason"] == "market_tsmom_signal_already_traded_today"


def test_live_takeover_can_disable_new_entries_without_masking_open_position(
    monkeypatch,
) -> None:
    now = datetime(2026, 8, 1, 0, 5, tzinfo=timezone.utc)
    candidate, _ = build_market_tsmom_shadow_candidate(
        FakeClient(now), _snapshot(now), {}, now, sleep_fn=lambda _: None
    )
    assert candidate is not None
    monkeypatch.setattr(
        market_tsmom_module,
        "current_market_tsmom_candidate",
        lambda _now=None: candidate,
    )
    config = {"market_tsmom_live_new_entries_enabled": False}

    blocked = build_market_tsmom_live_decision(
        config,
        {},
        {"equity": 20.0, "available_balance": 20.0, "positions": []},
        now,
    )
    assert blocked["action"] == "WAIT"
    assert blocked["reason"] == "market_tsmom_new_entries_disabled"

    occupied = build_market_tsmom_live_decision(
        config,
        {},
        {
            "equity": 20.0,
            "available_balance": 10.0,
            "positions": [{"symbol": "ETHUSDT", "positionAmt": "0.01"}],
        },
        now,
    )
    assert occupied["action"] == "WAIT"
    assert occupied["reason"] == "market_tsmom_position_already_open"


def test_live_takeover_rejects_a_stale_daily_entry(monkeypatch) -> None:
    signal_time = datetime(2026, 8, 1, 0, 5, tzinfo=timezone.utc)
    candidate, _ = build_market_tsmom_shadow_candidate(
        FakeClient(signal_time),
        _snapshot(signal_time),
        {},
        signal_time,
        sleep_fn=lambda _: None,
    )
    assert candidate is not None
    monkeypatch.setattr(
        market_tsmom_module,
        "current_market_tsmom_candidate",
        lambda _now=None: candidate,
    )

    decision = build_market_tsmom_live_decision(
        {},
        {},
        {"equity": 20.0, "available_balance": 20.0, "positions": []},
        signal_time + timedelta(hours=3),
    )

    assert decision["action"] == "WAIT"
    assert decision["reason"] == "market_tsmom_entry_window_expired"


def test_current_fixed_hold_position_is_not_managed_with_legacy_trailing(
    monkeypatch,
) -> None:
    now = datetime.now(timezone.utc)
    state = {
        "runtime_protection_positions": {
            "BNBUSDT:LONG": {
                "opened_at": now.isoformat(),
                "max_hold_seconds": 5 * 24 * 3600,
                "strategy_family": STRATEGY_FAMILY,
                "strategy_version": STRATEGY_VERSION,
            }
        }
    }

    result = runner_module.manage_market_tsmom_live_position(
        object(),
        {"market_tsmom_live_enabled": True},
        state,
        {"positions": [{"symbol": "BNBUSDT", "positionAmt": "0.02"}]},
    )

    assert result == {
        "managed": True,
        "closed": False,
        "reason": "fixed_time_hold_active",
    }


def test_legacy_position_remains_managed_after_v4_release(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    saved = []
    monkeypatch.setattr(
        runner_module,
        "market_tsmom_consensus_status",
        lambda: {
            "status": "no_signal",
            "signal_boundary": now.replace(
                hour=0, minute=0, second=0, microsecond=0
            ).isoformat(),
        },
    )
    monkeypatch.setattr(
        runner_module,
        "close_rotation_position",
        lambda _client, position: {"closed": position["symbol"]},
    )
    monkeypatch.setattr(runner_module, "record_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner_module, "save_state", lambda payload: saved.append(payload))
    state = {
        "runtime_protection_positions": {
            "ETHUSDT:LONG": {
                "opened_at": now.isoformat(),
                "max_hold_seconds": 20 * 24 * 3600,
                "strategy_family": STRATEGY_FAMILY,
                "strategy_version": "s0_market_tsmom_28_56_trailing_v3",
            }
        }
    }

    result = runner_module.manage_market_tsmom_live_position(
        object(),
        {"market_tsmom_live_enabled": True},
        state,
        {"positions": [{"symbol": "ETHUSDT", "positionAmt": "0.01"}]},
    )

    assert result["closed"] is True
    assert result["reason"] == "market_signal_off"
    assert saved[-1] == {"runtime_protection_positions": {}}


def test_legacy_position_rotates_only_when_fresh_v4_is_executable(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    saved = []
    events = []
    monkeypatch.setattr(
        runner_module,
        "build_market_tsmom_live_decision",
        lambda _config, _state, account: {
            "action": "OPEN_LONG",
            "symbol": "BNBUSDT",
            "risk_pct": 11.7,
            "projected_positions": account["positions"],
            "projected_available": account["available_balance"],
        },
    )
    monkeypatch.setattr(
        runner_module,
        "close_rotation_position",
        lambda _client, position: {"closed": position["symbol"]},
    )
    monkeypatch.setattr(runner_module, "save_state", lambda payload: saved.append(payload))
    monkeypatch.setattr(
        runner_module,
        "record_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    state = {
        "runtime_protection_positions": {
            "ETHUSDT:LONG": {
                "opened_at": now.isoformat(),
                "max_hold_seconds": 20 * 24 * 3600,
                "strategy_family": STRATEGY_FAMILY,
                "strategy_version": "s0_market_tsmom_28_56_trailing_v3",
            }
        }
    }

    result = runner_module.manage_market_tsmom_live_position(
        object(),
        {"market_tsmom_live_enabled": True},
        state,
        {
            "equity": 15.0,
            "available_balance": 3.0,
            "positions": [{"symbol": "ETHUSDT", "positionAmt": "0.01"}],
        },
    )

    assert result["closed"] is True
    assert result["reason"] == "legacy_strategy_replaced"
    assert result["replacement"] == {"symbol": "BNBUSDT", "risk_pct": 11.7}
    assert saved[-1] == {"runtime_protection_positions": {}}
    assert events[-1][0][1] == "market_tsmom_live_migration"


def test_legacy_position_is_kept_when_v4_replacement_is_not_executable(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        runner_module,
        "build_market_tsmom_live_decision",
        lambda _config, _state, _account: {
            "action": "WAIT",
            "reason": "market_tsmom_entry_window_expired",
        },
    )
    monkeypatch.setattr(
        runner_module,
        "market_tsmom_consensus_status",
        lambda: {
            "status": "candidate_ready",
            "signal_boundary": now.replace(
                hour=0, minute=0, second=0, microsecond=0
            ).isoformat(),
        },
    )
    monkeypatch.setattr(runner_module, "current_market_tsmom_candidate", lambda: None)
    state = {
        "runtime_protection_positions": {
            "ETHUSDT:LONG": {
                "opened_at": now.isoformat(),
                "max_hold_seconds": 20 * 24 * 3600,
                "strategy_family": STRATEGY_FAMILY,
                "strategy_version": "s0_market_tsmom_28_56_trailing_v3",
            }
        },
        "market_tsmom_live_management_day": now.date().isoformat(),
    }

    result = runner_module.manage_market_tsmom_live_position(
        object(),
        {"market_tsmom_live_enabled": True},
        state,
        {
            "equity": 15.0,
            "available_balance": 3.0,
            "positions": [{"symbol": "ETHUSDT", "positionAmt": "0.01"}],
        },
    )

    assert result == {
        "managed": False,
        "reason": "no_new_daily_stop",
    }
