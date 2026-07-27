from datetime import datetime, timezone

from app.s0_continuous_permit import s0_continuous_permit_status
from app.state_store import save_state


def _config() -> dict:
    return {
        "opportunity_v4_strategy_version": "v4.5",
        "s0_continuous_permit_enabled": True,
        "hard_stop_equity": 5.0,
        "_stage_route": {"stage": "S0"},
    }


def _row(trade_id: int, pnl: float, commission: float = 0.05) -> dict:
    return {
        "id": trade_id,
        "close_time": trade_id * 1000,
        "net_pnl": pnl,
        "commission": commission,
        "funding_fee": 0.0,
    }


def test_losses_reduce_position_without_revoking_entries(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_state({"daily_start_equity": 20.0, "daily_session_date": "2026-07-20"})

    status = s0_continuous_permit_status(
        _config(),
        equity=17.0,
        live_rows=[_row(3, -0.5), _row(2, -0.5), _row(1, -0.5)],
        now=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )

    assert status["allowed"] is True
    assert status["status"] == "position_penalty"
    assert status["consecutive_losses"] == 3
    assert status["risk_multiplier"] == 0.25


def test_effective_wins_restore_one_level_then_full(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_state({"daily_start_equity": 20.0, "daily_session_date": "2026-07-20"})
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    s0_continuous_permit_status(_config(), equity=19.0, live_rows=[_row(1, -0.5), _row(2, -0.5)], now=now)

    first = s0_continuous_permit_status(
        _config(), equity=19.5, live_rows=[_row(3, 0.20), _row(2, -0.5), _row(1, -0.5)], now=now
    )
    second = s0_continuous_permit_status(
        _config(), equity=20.0, live_rows=[_row(4, 0.20), _row(3, 0.20), _row(2, -0.5)], now=now
    )

    assert first["risk_multiplier"] == 0.75
    assert second["risk_multiplier"] == 1.0
    assert second["status"] == "normal"


def test_daily_drawdown_is_the_loss_pause_boundary(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_state({"daily_start_equity": 20.0, "daily_session_date": "2026-07-20"})

    status = s0_continuous_permit_status(
        {**_config(), "stage_s0_daily_loss_stop_enabled": True},
        equity=14.0,
        live_rows=[],
        now=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )

    assert status["daily_drawdown_pct"] == 30.0
    assert status["allowed"] is False
    assert status["status"] == "daily_paused"
    assert status["risk_multiplier"] == 0.0


def test_daily_drawdown_only_penalizes_results_when_s0_daily_stop_is_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_state({"daily_start_equity": 20.0, "daily_session_date": "2026-07-20"})

    status = s0_continuous_permit_status(
        {**_config(), "stage_s0_daily_loss_stop_enabled": False},
        equity=8.0,
        live_rows=[_row(1, -0.5)],
        now=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )

    assert status["daily_drawdown_pct"] == 60.0
    assert status["daily_loss_stop_enabled"] is False
    assert status["allowed"] is True
    assert status["risk_multiplier"] == 0.75


def test_five_usdt_hard_stop_remains_unconditional(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_state({"daily_start_equity": 20.0, "daily_session_date": "2026-07-20"})

    status = s0_continuous_permit_status(
        _config(), equity=5.0, live_rows=[], now=datetime(2026, 7, 20, tzinfo=timezone.utc)
    )

    assert status["allowed"] is False
    assert status["status"] == "hard_stop"
