from __future__ import annotations

from app import runner
from app.runner import enforce_hard_stop, is_min_notional_rejection, track_runtime_position
from app.state_store import load_state
from app.trading_engine import is_reduce_only_rejection


def test_min_notional_rejection_is_nonfatal_exchange_rejection():
    exc = RuntimeError('Binance signed API 400: {"code":-4164,"msg":"Order\'s notional must be no smaller than 5"}')

    assert is_min_notional_rejection(exc) is True
    assert is_min_notional_rejection(RuntimeError("timestamp outside recvWindow")) is False


def test_reduce_only_rejection_can_be_recovered_when_position_is_gone():
    exc = RuntimeError('Binance signed API 400: {"code":-2022,"msg":"ReduceOnly Order is rejected."}')

    assert is_reduce_only_rejection(exc) is True
    assert is_reduce_only_rejection(RuntimeError("timestamp outside recvWindow")) is False


def test_hard_stop_persists_terminal_state_without_live_api_calls(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))

    result = enforce_hard_stop(
        object(),
        {"hard_stop_equity": 5, "dry_run": True},
        {"equity": 4.9, "positions": []},
    )

    assert result["triggered"] is True
    assert load_state()["bot_status"] == "hard_stopped"
    assert load_state()["hard_stop_triggered"] is True


def test_open_signal_executes_through_fresh_account_guard(monkeypatch):
    class Client:
        def account_live(self):
            return {
                "totalWalletBalance": "30",
                "totalUnrealizedProfit": "0",
                "availableBalance": "30",
                "positions": [],
            }

    monkeypatch.setattr(
        runner,
        "execute_stage1_market_order",
        lambda client, decision, config: {"mode": "live_test", "symbol": decision["symbol"]},
    )

    result = runner.execute_with_freshness_guard(
        Client(),
        {"action": "OPEN_LONG", "symbol": "SOLUSDT"},
        {},
        {"positions": []},
    )

    assert result == {"mode": "live_test", "symbol": "SOLUSDT"}


def test_open_signal_blocks_when_available_balance_changed(monkeypatch):
    class Client:
        def account_live(self):
            return {
                "totalWalletBalance": "30",
                "totalUnrealizedProfit": "0",
                "availableBalance": "30",
                "positions": [],
            }

    monkeypatch.setattr(
        runner,
        "execute_stage1_market_order",
        lambda client, decision, config: {"mode": "should_not_execute"},
    )
    result = runner.execute_with_freshness_guard(
        Client(),
        {"action": "OPEN_LONG", "symbol": "SOLUSDT", "full_bet_sizing": {"sizing_available_balance": 3.0}},
        {"account_projection_balance_mismatch_pct": 2.0},
        {"positions": []},
    )

    assert result["reason"] == "stale_available_balance"


def test_track_runtime_position_uses_v473_candidate_protection(monkeypatch):
    saved = {}
    monkeypatch.setattr(runner, "load_state", lambda: {"runtime_protection_positions": {}})
    monkeypatch.setattr(runner, "save_state", lambda update: saved.update(update) or update)

    track_runtime_position(
        {
            "symbol": "SOLUSDT",
            "direction": "LONG",
            "signal": {},
            "candidate": {
                "strategy_family": "extreme_v4_roll",
                "opportunity_v4": {
                    "strategy_version": "v4.7.3",
                    "protection_profile": {
                        "max_hold_seconds": 480,
                        "stagnation_seconds": 180,
                        "stagnation_min_profit_pct": 0.12,
                        "fast_invalid_seconds": 120,
                    },
                },
            },
        }
    )

    tracked = saved["runtime_protection_positions"]["SOLUSDT:LONG"]
    assert tracked["max_hold_seconds"] == 480
    assert tracked["stagnation_seconds"] == 180
    assert tracked["stagnation_min_profit_pct"] == 0.12
    assert tracked["fast_invalid_seconds"] == 120


def test_track_runtime_position_merges_partial_signal_with_v4_candidate_profile(monkeypatch):
    saved = {}
    monkeypatch.setattr(runner, "load_state", lambda: {"runtime_protection_positions": {}})
    monkeypatch.setattr(runner, "save_state", lambda update: saved.update(update) or update)

    decision = {
        "symbol": "SOLUSDT",
        "direction": "LONG",
        "signal": {
            "atr": 2.5,
            "protection_profile": {
                "max_hold_seconds": 1200,
                "break_even_atr": 0.45,
            },
        },
        "candidate": {
            "strategy_family": "extreme_v4_roll",
            "opportunity_v4": {
                "strategy_version": "v4.7.4",
                "protection_profile": {
                    "max_hold_seconds": 480,
                    "stagnation_seconds": 180,
                    "stagnation_min_profit_pct": 0.12,
                    "fast_invalid_seconds": 120,
                },
            },
        },
    }

    track_runtime_position(decision)

    tracked = saved["runtime_protection_positions"]["SOLUSDT:LONG"]
    assert tracked["max_hold_seconds"] == 480
    assert tracked["stagnation_seconds"] == 180
    assert tracked["break_even_atr"] == 0.45
    assert tracked["entry_atr"] == 2.5
    assert tracked["protection_profile_source"] == "v4_candidate_merged"
    assert decision["signal"]["protection_profile"]["max_hold_seconds"] == 480
