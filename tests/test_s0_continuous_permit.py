from datetime import datetime, timedelta, timezone

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


def test_v50_three_losses_cool_down_then_restore_at_twelve_percent(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    config = {
        **_config(),
        "opportunity_v4_strategy_version": "v5.0-s30",
        "opportunity_v50_loss_3_cooldown_minutes": 20.0,
        "opportunity_v50_min_risk_pct": 12.0,
    }
    now = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    close_ms = int(now.timestamp() * 1000)
    rows = [_row(1, -0.5), _row(2, -0.5), _row(3, -0.5)]
    for index, row in enumerate(rows):
        row["close_time"] = close_ms - (2 - index) * 1000

    cooling = s0_continuous_permit_status(
        config,
        equity=17.0,
        live_rows=rows,
        now=now + timedelta(minutes=1),
    )
    assert cooling["allowed"] is False
    assert cooling["status"] == "loss_cooldown"
    assert cooling["loss_cooldown_active"] is True

    restored = s0_continuous_permit_status(
        config,
        equity=17.0,
        live_rows=rows,
        now=now + timedelta(minutes=21),
    )
    assert restored["allowed"] is True
    assert restored["status"] == "baseline_recovery"
    assert restored["risk_cap_pct"] == 12.0
    assert restored["risk_multiplier"] == 1.0


def test_v511_starts_at_half_risk_and_penalizes_first_two_losses(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    config = {
        **_config(),
        "opportunity_v4_strategy_version": "v5.1.1",
        "opportunity_v511_initial_multiplier": 0.50,
        "opportunity_v511_loss_1_multiplier": 0.40,
        "opportunity_v511_loss_2_multiplier": 0.25,
    }
    now = datetime(2026, 7, 29, tzinfo=timezone.utc)

    initial = s0_continuous_permit_status(config, equity=10.0, live_rows=[], now=now)
    first_loss = s0_continuous_permit_status(
        config,
        equity=9.5,
        live_rows=[_row(1, -0.5)],
        now=now + timedelta(minutes=1),
    )
    second_loss = s0_continuous_permit_status(
        config,
        equity=9.0,
        live_rows=[_row(1, -0.5), _row(2, -0.5)],
        now=now + timedelta(minutes=2),
    )

    assert initial["risk_multiplier"] == 0.5
    assert initial["status"] == "initial_exploration"
    assert first_loss["risk_multiplier"] == 0.4
    assert second_loss["risk_multiplier"] == 0.25


def test_v53_keeps_loss_penalty_proportional(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    config = {
        **_config(),
        "opportunity_v4_strategy_version": "v5.3",
        "opportunity_v50_loss_1_multiplier": 0.80,
        "opportunity_v50_loss_2_multiplier": 0.60,
    }
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)

    first_loss = s0_continuous_permit_status(
        config,
        equity=9.5,
        live_rows=[_row(1, -0.5)],
        now=now,
    )
    second_loss = s0_continuous_permit_status(
        config,
        equity=9.0,
        live_rows=[_row(1, -0.5), _row(2, -0.5)],
        now=now + timedelta(minutes=1),
    )

    assert first_loss["allowed"] is True
    assert first_loss["risk_multiplier"] == 0.80
    assert second_loss["risk_multiplier"] == 0.60


def test_v53_starts_with_full_independent_opportunity_multiplier(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))

    status = s0_continuous_permit_status(
        {**_config(), "opportunity_v4_strategy_version": "v5.3"},
        equity=10.0,
        live_rows=[],
        now=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )

    assert status["status"] == "initial_exploration"
    assert status["risk_multiplier"] == 1.0
