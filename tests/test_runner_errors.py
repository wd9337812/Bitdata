from __future__ import annotations

from app import runner
from app.runner import enforce_hard_stop, is_min_notional_rejection
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
