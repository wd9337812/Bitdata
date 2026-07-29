from datetime import datetime, timedelta, timezone

from app.local_circuit import (
    candidate_local_circuit_status,
    local_circuit_state,
    reconcile_v4_local_circuit,
    record_v4_live_open,
)


def _candidate(symbol: str = "ALTUSDT") -> dict:
    return {
        "symbol": symbol,
        "direction": "LONG",
        "entry_type": "pullback",
        "strategy_family": "extreme_v4_roll",
        "strategy_version": "v4.3.1",
        "signal": {"signal": "LONG", "entry_phase": "RETEST"},
        "market_state": {"state": "quiet"},
        "market_structure": {
            "market_regime": "quiet",
            "setup_type": "pullback",
            "entry_phase": "RETEST",
        },
    }


def _shadow(index: int, net_pct: float, symbol: str, *, lane: str = "core_canary") -> dict:
    return {
        "id": index,
        "symbol": symbol,
        "closed_at": (datetime(2026, 7, 19, tzinfo=timezone.utc) + timedelta(minutes=index)).isoformat(),
        "direction": "LONG",
        "entry_type": "pullback",
        "market_regime": "quiet",
        "entry_phase": "RETEST",
        "admission_lane": lane,
        "net_pct": net_pct,
    }


def test_two_same_cohort_live_losses_only_block_that_cohort(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    release_id = "extreme_v4_roll@v4.3.1"
    candidate = _candidate()
    now = datetime(2026, 7, 19, 1, 0, tzinfo=timezone.utc)
    protected = {"mode": "live", "stop_order": {"id": 1}, "take_profit_order": {"id": 2}}

    for index in range(2):
        opened_at = now + timedelta(minutes=index * 10)
        record_v4_live_open(
            {"symbol": "ALTUSDT", "direction": "LONG", "candidate": candidate},
            protected,
            now=opened_at,
        )
        reconcile_v4_local_circuit(
            {"opportunity_v431_local_live_loss_streak": 2},
            release_id=release_id,
            live_rows=[
                {
                    "symbol": "ALTUSDT",
                    "direction": "LONG",
                    "close_time": int((opened_at + timedelta(minutes=2)).timestamp() * 1000),
                    "net_pnl": -0.2,
                }
            ],
            now=opened_at + timedelta(minutes=2),
        )

    blocked = candidate_local_circuit_status(
        candidate,
        [],
        {"opportunity_v4_strategy_version": "v4.3.1"},
        state=local_circuit_state(release_id),
    )
    other = _candidate("OTHERUSDT")
    other["direction"] = "SHORT"
    other["signal"]["signal"] = "SHORT"
    clear = candidate_local_circuit_status(
        other,
        [],
        {"opportunity_v4_strategy_version": "v4.3.1"},
        state=local_circuit_state(release_id),
    )

    assert blocked["blocked"] is True
    assert blocked["reason"] == "two_consecutive_live_losses"
    assert clear["blocked"] is False


def test_local_shadow_circuit_restores_after_fresh_positive_cross_symbol_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    losses = [_shadow(index, -0.2, f"LOSS{index}USDT", lane="shadow_only") for index in range(1, 9)]
    wins = [_shadow(index, 0.3, f"WIN{index % 3}USDT") for index in range(9, 17)]

    status = candidate_local_circuit_status(
        _candidate(),
        losses + wins,
        {"opportunity_v4_strategy_version": "v4.3.1"},
    )

    assert status["blocked"] is False
    assert status["restored"] is True
    assert status["shadow"]["restored_at"]


def test_v511_tracks_v5_losses_across_setup_names(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    release_id = "extreme_v5_roll@v5.1.1"
    now = datetime.now(timezone.utc) - timedelta(minutes=20)
    protected = {"mode": "live", "stop_order": {"id": 1}, "take_profit_order": {"id": 2}}

    for index, setup in enumerate(("prebreakout", "pullback")):
        candidate = _candidate("ZILUSDT")
        candidate.update(
            {
                "entry_type": setup,
                "strategy_family": "extreme_v5_roll",
                "strategy_version": "v5.1.1",
            }
        )
        candidate["market_structure"]["setup_type"] = setup
        opened_at = now + timedelta(minutes=index * 10)
        record_v4_live_open(
            {"symbol": "ZILUSDT", "direction": "LONG", "candidate": candidate},
            protected,
            now=opened_at,
        )
        reconcile_v4_local_circuit(
            {"opportunity_v431_local_live_loss_streak": 2},
            release_id=release_id,
            live_rows=[
                {
                    "symbol": "ZILUSDT",
                    "direction": "LONG",
                    "close_time": int((opened_at + timedelta(minutes=2)).timestamp() * 1000),
                    "net_pnl": -0.2,
                }
            ],
            now=opened_at + timedelta(minutes=2),
        )

    candidate = _candidate("ZILUSDT")
    candidate.update(
        {
            "entry_type": "breakout",
            "strategy_family": "extreme_v5_roll",
            "strategy_version": "v5.1.1",
        }
    )
    candidate["market_structure"]["setup_type"] = "breakout"
    status = candidate_local_circuit_status(
        candidate,
        [],
        {
            "opportunity_v4_strategy_version": "v5.1.1",
            "opportunity_v511_same_direction_dedupe_minutes": 120,
            "opportunity_v511_same_direction_hard_losses": 2,
        },
        state=local_circuit_state(release_id),
    )

    assert status["episode"]["loss_streak"] == 2
    assert status["episode"]["within_dedupe_window"] is True
    assert status["release_id"] == release_id


def test_reconcile_recovers_v5_loss_when_open_context_is_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    release_id = "extreme_v5_roll@v5.1.1"
    closed_at = datetime(2026, 7, 29, 3, 9, tzinfo=timezone.utc)

    state = reconcile_v4_local_circuit(
        {"opportunity_v431_local_live_loss_streak": 2},
        release_id=release_id,
        live_rows=[
            {
                "symbol": "KAITOUSDT",
                "direction": "LONG",
                "entry_type": "breakout",
                "market_regime": "mixed",
                "entry_phase": "TRIGGERED",
                "close_time": int(closed_at.timestamp() * 1000),
                "net_pnl": -0.53,
            }
        ],
        now=closed_at,
    )

    assert state["cohorts"]["mixed:LONG:breakout:TRIGGERED"]["live_loss_streak"] == 1
    assert state["symbol_episodes"]["KAITOUSDT:LONG"]["loss_streak"] == 1
