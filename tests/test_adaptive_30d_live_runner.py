from datetime import datetime, timedelta, timezone

import app.runner as runner
from app.adaptive_30d_momentum_shadow import (
    LIVE_STRATEGY_VERSION,
    STRATEGY_FAMILY,
)


def test_track_runtime_position_resets_s0_progress_only_after_live_open(monkeypatch):
    saved = []
    monkeypatch.setattr(runner, "load_state", lambda: {})
    monkeypatch.setattr(runner, "save_state", lambda updates: saved.append(updates))
    decision = {
        "symbol": "ALTUSDT",
        "direction": "LONG",
        "equity": 15.0,
        "risk_pct": 15.0,
        "leverage": 2,
        "candidate": {
            "strategy_family": STRATEGY_FAMILY,
            "strategy_version": LIVE_STRATEGY_VERSION,
            "signal": {
                "atr": 1.0,
                "protection_profile": {"max_hold_seconds": 168 * 3600},
            },
        },
        "signal": {
            "atr": 1.0,
            "protection_profile": {"max_hold_seconds": 168 * 3600},
        },
    }
    result = {
        "entry_order": {"executedQty": "1"},
        "stop_order": {"algoId": 1},
        "take_profit_order": {"algoId": 2},
    }

    runner.track_runtime_position(decision, result)

    update = saved[-1]
    assert update["adaptive_30d_live_progress_reset_done"] is True
    assert update["target_phase_a_start_equity"] == 15.0
    assert update["target_start_equity"] == 15.0
    assert datetime.fromisoformat(update["target_phase_a_start_time"]).tzinfo == timezone.utc


def test_handoff_does_not_close_without_fresh_executable_candidate(monkeypatch):
    monkeypatch.setattr(
        runner,
        "build_adaptive_30d_live_decision",
        lambda *args, **kwargs: {"action": "WAIT", "reason": "no_signal"},
    )
    state = {
        "runtime_protection_positions": {
            "ETHUSDT:LONG": {
                "strategy_family": runner.MARKET_TSMOM_FAMILY,
                "strategy_version": "legacy",
            }
        }
    }
    account = {
        "equity": 15.0,
        "available_balance": 3.0,
        "positions": [{"symbol": "ETHUSDT", "positionAmt": "0.01"}],
    }

    result = runner.manage_adaptive_30d_handoff(
        object(), {"adaptive_30d_live_enabled": True}, state, account
    )

    assert result["managed"] is False
    assert result["reason"] == "no_fresh_executable_adaptive_replacement"


def test_handoff_closes_market_position_only_for_fresh_adaptive_replacement(monkeypatch):
    closed = []
    saved = []
    monkeypatch.setattr(
        runner,
        "build_adaptive_30d_live_decision",
        lambda *args, **kwargs: {
            "action": "OPEN_SHORT",
            "symbol": "ALTUSDT",
            "direction": "SHORT",
            "risk_pct": 15.0,
        },
    )
    monkeypatch.setattr(
        runner,
        "close_rotation_position",
        lambda client, position: closed.append(position) or {"status": "FILLED"},
    )
    monkeypatch.setattr(runner, "save_state", lambda update: saved.append(update))
    monkeypatch.setattr(runner, "record_event", lambda *args, **kwargs: None)
    state = {
        "runtime_protection_positions": {
            "ETHUSDT:LONG": {
                "strategy_family": runner.MARKET_TSMOM_FAMILY,
                "strategy_version": "legacy",
            }
        }
    }
    account = {
        "equity": 15.0,
        "available_balance": 3.0,
        "positions": [{"symbol": "ETHUSDT", "positionAmt": "0.01"}],
    }

    result = runner.manage_adaptive_30d_handoff(
        object(), {"adaptive_30d_live_enabled": True}, state, account
    )

    assert result["managed"] is True
    assert result["closed"] is True
    assert result["reason"] == "fresh_adaptive_replacement"
    assert closed == [account["positions"][0]]
    assert saved[-1]["runtime_protection_positions"] == {}


def test_adaptive_position_closes_only_after_max_hold(monkeypatch):
    closed = []
    saved = []
    monkeypatch.setattr(
        runner,
        "close_rotation_position",
        lambda client, position: closed.append(position) or {"status": "FILLED"},
    )
    monkeypatch.setattr(runner, "save_state", lambda update: saved.append(update))
    monkeypatch.setattr(runner, "record_event", lambda *args, **kwargs: None)
    state = {
        "runtime_protection_positions": {
            "ALTUSDT:LONG": {
                "strategy_family": STRATEGY_FAMILY,
                "strategy_version": LIVE_STRATEGY_VERSION,
                "opened_at": (
                    datetime.now(timezone.utc) - timedelta(days=8)
                ).isoformat(),
                "max_hold_seconds": 7 * 24 * 3600,
            }
        }
    }
    account = {
        "positions": [{"symbol": "ALTUSDT", "positionAmt": "1"}],
    }

    result = runner.manage_adaptive_30d_live_position(
        object(), {"adaptive_30d_live_enabled": True}, state, account
    )

    assert result["managed"] is True
    assert result["closed"] is True
    assert result["reason"] == "max_hold"
    assert closed == [account["positions"][0]]
    assert saved[-1]["runtime_protection_positions"] == {}
