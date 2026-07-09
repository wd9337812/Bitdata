from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.live_reaction import apply_live_reaction_to_candidate, score_reaction_records


def _record(net: float, close_offset_seconds: int, symbol: str = "VANRYUSDT", direction: str = "LONG") -> dict:
    now = datetime(2026, 7, 9, 0, 40, tzinfo=timezone.utc)
    close = now - timedelta(seconds=close_offset_seconds)
    return {
        "symbol": symbol,
        "direction": direction,
        "open_time": int((close - timedelta(seconds=45)).timestamp() * 1000),
        "close_time": int(close.timestamp() * 1000),
        "open_notional": 100,
        "net_pnl": net,
        "commission": 0.05,
        "hold_seconds": 45,
    }


def test_one_loss_reduces_risk_without_blocking():
    now = datetime(2026, 7, 9, 0, 40, tzinfo=timezone.utc)
    state = score_reaction_records([_record(-2.0, 30)], {}, equity=50, now=now)

    assert state["status"] == "cooldown"
    assert state["risk_multiplier"] == 0.4
    assert state["consecutive_losses"] == 1
    assert state["cooldown_until"]


def test_two_losses_ban_same_symbol_direction():
    now = datetime(2026, 7, 9, 0, 40, tzinfo=timezone.utc)
    state = score_reaction_records([_record(-1.0, 90), _record(-2.0, 30)], {}, equity=50, now=now)

    assert state["status"] == "banned"
    assert state["risk_multiplier"] == 0
    assert state["consecutive_losses"] == 2
    assert state["ban_until"]


def test_profit_tail_reduces_chasing_after_two_wins():
    now = datetime(2026, 7, 9, 0, 40, tzinfo=timezone.utc)
    state = score_reaction_records([_record(1.0, 120), _record(1.5, 20)], {}, equity=50, now=now)

    assert state["status"] == "tail_guard"
    assert state["risk_multiplier"] == 0.5
    assert state["consecutive_wins"] == 2


def test_tail_loss_bans_after_profit_chase_failure():
    now = datetime(2026, 7, 9, 0, 40, tzinfo=timezone.utc)
    state = score_reaction_records([_record(1.0, 180), _record(1.5, 120), _record(-2.0, 20)], {}, equity=50, now=now)

    assert state["status"] == "banned"
    assert state["risk_multiplier"] == 0
    assert "追尾亏损" in state["reason"]


def test_large_single_loss_extends_same_direction_ban():
    now = datetime(2026, 7, 9, 0, 40, tzinfo=timezone.utc)
    state = score_reaction_records(
        [_record(-6.0, 30)],
        {
            "live_reaction_single_loss_ban_equity_pct": 10,
            "live_reaction_single_loss_ban_minutes": 180,
        },
        equity=50,
        now=now,
    )

    assert state["status"] == "banned"
    assert state["risk_multiplier"] == 0
    assert state["payload"]["largest_single_loss_pct"] == 12
    assert "最大单笔亏损" in state["reason"]


def test_profit_giveback_bans_after_winning_session_turns_over():
    now = datetime(2026, 7, 9, 0, 40, tzinfo=timezone.utc)
    state = score_reaction_records(
        [_record(8.0, 600), _record(7.0, 500), _record(-8.0, 220), _record(-8.0, 30)],
        {
            "live_reaction_giveback_min_profit_usdt": 1,
            "live_reaction_giveback_ban_pct": 80,
            "live_reaction_giveback_ban_minutes": 180,
            "live_reaction_single_loss_ban_equity_pct": 30,
            "live_reaction_recent_loss_equity_pct": 99,
            "live_reaction_symbol_direction_daily_loss_pct": 99,
        },
        equity=50,
        now=now,
    )

    assert state["status"] == "banned"
    assert state["payload"]["day_peak_profit"] == 15
    assert state["payload"]["day_profit_giveback_pct"] > 100
    assert "回吐" in state["reason"]


def test_apply_live_reaction_blocks_candidate(monkeypatch):
    monkeypatch.setattr(
        "app.live_reaction.live_reaction_for",
        lambda symbol, direction, config: {
            "enabled": True,
            "status": "banned",
            "status_label": "暂停同向",
            "risk_multiplier": 0,
            "score_penalty": 24,
            "ban_active": True,
            "reason": "同方向两连亏",
            "ban_until": "2026-07-09T01:10:00+00:00",
        },
    )
    candidate = {
        "symbol": "VANRYUSDT",
        "direction": "LONG",
        "passed": True,
        "score": 90,
        "risk_pct": 55,
    }

    adjusted = apply_live_reaction_to_candidate(candidate, {"live_reaction_enabled": True})

    assert adjusted["passed"] is False
    assert adjusted["reason"] == "live_reaction_ban"
    assert adjusted["score"] == 66
