from datetime import datetime, timezone

from app.s0_daily_profit_lock import s0_daily_profit_lock_status
from app.state_store import load_state


def _config() -> dict:
    return {
        "_stage_route": {"stage": "S0"},
        "stage_s0_daily_profit_lock_enabled": True,
        "stage_s0_daily_profit_target_pct": 40.0,
    }


def test_daily_profit_target_locks_only_when_flat(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    state = {
        "daily_start_equity": 20.0,
        "active_stage": "S0",
        "s0_daily_profit_lock_active": False,
    }
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)

    pending = s0_daily_profit_lock_status(
        _config(),
        state,
        {"equity": 29.0, "positions": [{"symbol": "SOLUSDT", "positionAmt": "1"}]},
        now=now,
        realized_net=8.0,
        closed_trades=3,
    )
    locked = s0_daily_profit_lock_status(
        _config(),
        load_state(),
        {"equity": 28.0, "positions": []},
        now=now,
        realized_net=8.0,
        closed_trades=4,
    )

    assert pending["pending_flat"] is True
    assert pending["active"] is False
    assert locked["active"] is True
    assert locked["blocks_new_entries"] is True
    assert locked["target_usdt"] == 8.0
    assert locked["reset_at"] == "2026-07-28T00:00:00+00:00"


def test_daily_profit_lock_does_not_apply_outside_s0(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    status = s0_daily_profit_lock_status(
        {**_config(), "_stage_route": {"stage": "S1"}},
        {"daily_start_equity": 20.0, "active_stage": "S1"},
        {"equity": 40.0, "positions": []},
        realized_net=20.0,
        closed_trades=2,
    )

    assert status["enabled"] is False
    assert status["active"] is False
