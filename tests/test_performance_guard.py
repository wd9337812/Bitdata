from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from app.live_learning import init_live_learning_schema
from app.performance_guard import (
    apply_strategy_evidence_to_candidate,
    clear_performance_cache,
    global_performance_guard,
    observed_round_trip_cost_pct,
    strategy_evidence_for,
    update_release_equity_guard,
)
from app.shadow_trading import ensure_shadow_tables
from app.state_store import save_state
from app.telemetry import connect


def _seed_live(
    symbol: str,
    direction: str,
    values: list[float],
    *,
    commission: float = 0.01,
    version: str | None = None,
    role: str | None = None,
    family: str = "extreme_v3_roll",
) -> None:
    now = datetime.now(timezone.utc)
    with connect() as conn:
        for index, net in enumerate(values):
            close_time = int((now - timedelta(minutes=len(values) - index)).timestamp() * 1000)
            conn.execute(
                """
                INSERT INTO live_trade_records (
                    symbol, direction, open_time, close_time, open_notional, realized_pnl,
                    commission, funding_fee, net_pnl, hold_seconds, created_at
                ) VALUES (?, ?, ?, ?, 20, ?, ?, 0, ?, 60, ?)
                """,
                (symbol, direction, close_time - 60_000, close_time, net + commission, commission, net, now.isoformat()),
            )
            if version:
                conn.execute(
                    "UPDATE live_trade_records SET strategy_family = ?, strategy_version = ?, "
                    "strategy_role = ?, release_id = ? WHERE id = last_insert_rowid()",
                    (family, version, role or "active", f"{family}@{version}"),
                )
        conn.commit()


def _seed_shadow(
    symbol: str,
    direction: str,
    values: list[float],
    signal: str = "watch",
    *,
    version: str | None = None,
    role: str | None = None,
    family: str = "extreme_v3_roll",
    evidence_type: str = "decision",
    admission_lane: str | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    with connect() as conn:
        for index, net in enumerate(values):
            opened = now - timedelta(minutes=len(values) - index + 1)
            closed = opened + timedelta(minutes=1)
            dedupe_key = f"{symbol}:{direction}:{version or 'legacy'}:{role or 'legacy'}:{index}:{now.timestamp()}"
            conn.execute(
                """
                INSERT INTO shadow_trades (
                    dedupe_key, opened_at, closed_at, symbol, direction, signal_type, mode,
                    status, entry, stop, take_profit, last_price, notional, estimated_cost,
                    gross_pnl, net_pnl, outcome, expires_at, payload, evidence_type
                ) VALUES (?, ?, ?, ?, ?, ?, 'extreme_sprint', 'CLOSED', 1, 0.99, 1.01, 1,
                          20, 0.024, ?, ?, 'TIME_EXIT', ?, ?, ?)
                """,
                (
                    dedupe_key,
                    opened.isoformat(),
                    closed.isoformat(),
                    symbol,
                    direction,
                    signal,
                    net + 0.024,
                    net,
                    closed.isoformat(),
                    json.dumps({"admission_lane": admission_lane}) if admission_lane else "{}",
                    evidence_type,
                ),
            )
            if version:
                conn.execute(
                    "UPDATE shadow_trades SET strategy_family = ?, strategy_version = ?, "
                    "strategy_role = ?, release_id = ? WHERE dedupe_key = ?",
                    (family, version, role or "active", f"{family}@{version}", dedupe_key),
                )
        conn.commit()


def _prepare(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    clear_performance_cache()
    init_live_learning_schema()
    with connect() as conn:
        ensure_shadow_tables(conn)


def test_global_guard_pauses_when_live_and_shadow_are_both_bad(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    _seed_live("BADUSDT", "LONG", [-0.2] * 10)
    _seed_shadow("BADUSDT", "LONG", [-0.1] * 50)
    clear_performance_cache()

    status = global_performance_guard({}, 25)

    assert status["status"] == "hard_cooldown"
    assert status["guard_level"] == "hard"
    assert status["allowed"] is False
    assert status["rolling_losses"] == 10
    assert status["tail_losses"] == 10


def test_global_guard_allows_only_small_recovery_after_pause(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    _seed_live("BADUSDT", "LONG", [-0.2] * 10)
    _seed_shadow("BADUSDT", "LONG", [-0.1] * 50)
    clear_performance_cache()

    status = global_performance_guard(
        {"performance_guard_recovery_risk_multiplier": 0.2},
        25,
        now=datetime.now(timezone.utc) + timedelta(hours=2),
    )

    assert status["status"] == "risk_off"
    assert status["allowed"] is False
    assert status["risk_multiplier"] == 0.0


def test_peak_drawdown_can_recover_through_confirmed_shadow_probe(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    config = {
        "performance_guard_peak_drawdown_pct": 12,
        "performance_guard_recovery_shadow_trades": 20,
        "performance_recovery_confirm_closes": 3,
        "performance_recovery_confirm_minutes": 60,
        "performance_guard_recovery_level_2_multiplier": 0.4,
    }
    now = datetime.now(timezone.utc)
    _seed_live("GOODUSDT", "LONG", [0.1], version="v3.2", role="active")
    _seed_shadow("GOODUSDT", "LONG", [0.1] * 20, version="v3.2", role="active")
    with connect() as conn:
        conn.execute(
            "INSERT INTO equity_snapshots (ts, equity, available_balance, unrealized_pnl) "
            "VALUES (?, 100, 100, 0)",
            (now.isoformat(),),
        )
        conn.commit()

    first = global_performance_guard(config, 80, now=now + timedelta(hours=2))
    _seed_shadow("GOODUSDT", "LONG", [0.1], version="v3.2", role="active")
    clear_performance_cache()
    second = global_performance_guard(config, 80, now=now + timedelta(hours=2, minutes=1))
    _seed_shadow("GOODUSDT", "LONG", [0.1], version="v3.2", role="active")
    clear_performance_cache()
    permit = global_performance_guard(config, 80, now=now + timedelta(hours=2, minutes=2))

    assert first["peak_drawdown_severe"] is True
    assert first["status"] == "confirming"
    assert first["allowed"] is False
    assert second["allowed"] is False
    assert permit["status"] == "recovery_2"
    assert permit["allowed"] is True
    assert permit["risk_multiplier"] == 0.4


def test_severe_live_loss_pauses_even_when_shadow_is_not_bad(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    _seed_live("BADUSDT", "SHORT", [-0.4] * 9 + [0.01])
    _seed_shadow("GOODUSDT", "LONG", [0.1] * 50)
    clear_performance_cache()

    status = global_performance_guard({}, 20)

    assert status["live_severe"] is True
    assert status["shadow_bad"] is False
    assert status["allowed"] is False
    assert status["hard_risk_off"] is True
    assert "硬保护" in status["reason"]


def test_consecutive_losses_pause_before_aggregate_profit_turns_negative(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    _seed_live("STREAKUSDT", "LONG", [2.0, -0.2, -0.2, -0.2, -0.2])
    _seed_shadow("STREAKUSDT", "LONG", [0.1] * 20)
    clear_performance_cache()

    status = global_performance_guard({}, 20)

    assert status["live"]["net_pnl"] > 0
    assert status["tail_losses"] == 4
    assert status["severe_loss_streak"] is True
    assert status["live_severe"] is True
    assert status["allowed"] is False


def test_negative_shadow_and_live_evidence_caps_risk_and_blocks_reentry(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    _seed_live("BADUSDT", "SHORT", [-0.3, -0.2])
    _seed_shadow("BADUSDT", "SHORT", [-0.1] * 10)
    clear_performance_cache()

    evidence = strategy_evidence_for("BADUSDT", "SHORT", {})
    candidate = apply_strategy_evidence_to_candidate(
        {
            "symbol": "BADUSDT",
            "direction": "SHORT",
            "passed": True,
            "risk_pct": 15,
            "live_credit_adjustment": {"input_risk_pct": 10, "risk_multiplier": 1.5, "boost_qualified": True},
        },
        {},
    )

    assert evidence["agreement"] == "negative"
    assert evidence["boost_allowed"] is False
    assert candidate["risk_pct"] == 2.5
    assert candidate["passed"] is False
    assert candidate["reason"] == "strategy_evidence_reentry_cooldown"


def test_positive_evidence_is_required_before_credit_boost(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    _seed_live("GOODUSDT", "SHORT", [0.2] * 8)
    _seed_shadow("GOODUSDT", "SHORT", [0.1] * 30)
    clear_performance_cache()

    evidence = strategy_evidence_for("GOODUSDT", "SHORT", {})
    candidate = apply_strategy_evidence_to_candidate(
        {
            "symbol": "GOODUSDT",
            "direction": "SHORT",
            "passed": True,
            "risk_pct": 15,
            "live_credit_adjustment": {"input_risk_pct": 10, "risk_multiplier": 1.5, "boost_qualified": True},
        },
        {},
    )

    assert evidence["agreement"] == "positive"
    assert evidence["boost_allowed"] is True
    assert candidate["risk_pct"] == 11.5


def test_v3_evidence_uses_only_current_release_and_flags_bad_signal(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    _seed_shadow("PUMPUSDT", "LONG", [0.4] * 20, "v3_pullback", version="v3.1", role="archived")
    _seed_shadow("PUMPUSDT", "LONG", [-0.1] * 10, "v3_pullback", version="v3.2", role="active")
    clear_performance_cache()

    candidate = apply_strategy_evidence_to_candidate(
        {
            "symbol": "PUMPUSDT",
            "direction": "LONG",
            "strategy_family": "extreme_v3_roll",
            "strategy_version": "v3.2",
            "entry_type": "v3_pullback",
            "passed": True,
            "risk_pct": 10,
        },
        {"opportunity_v3_strategy_version": "v3.2"},
    )

    evidence = candidate["strategy_evidence"]
    assert evidence["release_scoped"] is True
    assert evidence["shadow"]["trades"] == 10
    assert evidence["shadow"]["net_pnl"] < 0
    assert evidence["signal"]["negative"] is True
    assert evidence["symbol_negative"] is True
    assert evidence["recovery_compatible"] is False
    assert candidate["risk_pct"] == 5.0


def test_observed_cost_uses_recent_live_fee_floor(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    _seed_live("COSTUSDT", "LONG", [0.1] * 4, commission=0.04)
    clear_performance_cache()

    cost = observed_round_trip_cost_pct({"observed_cost_safety_multiplier": 1.0})

    assert cost == 0.2


def test_global_guard_ignores_archived_profit_when_current_release_is_bad(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    _seed_live("OLDUSDT", "LONG", [1.0] * 10, version="v3.1", role="archived")
    _seed_live("NEWUSDT", "LONG", [-0.3] * 10, version="v3.2", role="active")
    _seed_shadow("OLDUSDT", "LONG", [0.5] * 50, version="v3.1", role="archived")
    _seed_shadow("NEWUSDT", "LONG", [-0.1] * 50, version="v3.2", role="active")
    clear_performance_cache()

    status = global_performance_guard(
        {"opportunity_v3_strategy_version": "v3.2", "performance_guard_current_release_only": True},
        25,
    )

    assert status["allowed"] is False
    assert status["live_evidence_scope"] == "extreme_v3_roll@v3.2"
    assert status["shadow_evidence_scope"] == "extreme_v3_roll@v3.2"
    assert status["live"]["net_pnl"] < 0
    assert status["shadow"]["net_pnl"] < 0


def test_one_current_release_trade_cannot_clear_legacy_safety_fallback(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    _seed_live("OLDUSDT", "LONG", [-0.4] * 10)
    _seed_live("NEWUSDT", "LONG", [0.2], version="v3.2", role="active")
    _seed_shadow("NEWUSDT", "LONG", [0.1] * 50, version="v3.2", role="active")
    clear_performance_cache()

    status = global_performance_guard(
        {
            "opportunity_v3_strategy_version": "v3.2",
            "performance_guard_current_release_only": True,
            "performance_recovery_current_live_warmup_trades": 8,
        },
        25,
        now=datetime.now(timezone.utc) + timedelta(hours=3),
    )

    assert status["release_warmup"] is True
    assert status["current_live"]["trades"] == 1
    assert status["allowed"] is False


def test_v4_live_release_does_not_inherit_v3_negative_gate(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    _seed_live("OLDUSDT", "LONG", [-0.4] * 10, version="v3.2", role="archived")
    _seed_shadow("OLDUSDT", "LONG", [-0.2] * 50, version="v3.2", role="archived")
    clear_performance_cache()

    status = global_performance_guard(
        {
            "opportunity_v4_live_enabled": True,
            "opportunity_v4_strategy_version": "v4.0",
            "performance_guard_current_release_only": True,
        },
        25,
    )

    assert status["active_strategy_family"] == "extreme_v4_roll"
    assert status["live_evidence_scope"] == "extreme_v4_roll@v4.0"
    assert status["shadow_evidence_scope"] == "extreme_v4_roll@v4.0:live_lanes"
    assert status["fallback_live"]["trades"] == 10
    assert status["fallback_live"]["net_pnl"] < 0
    assert status["release_warmup"] is False
    assert status["allowed"] is True


def test_v41_canary_without_live_closes_does_not_renew_cooldown_forever(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    monkeypatch.setenv("APP_STATE_PATH", str(tmp_path / "state.json"))
    now = datetime.now(timezone.utc)
    with connect() as conn:
        conn.execute(
            "INSERT INTO equity_snapshots (ts, equity, available_balance, unrealized_pnl) "
            "VALUES (?, 100, 100, 0)",
            (now.isoformat(),),
        )
        conn.commit()

    status = global_performance_guard(
        {
            "opportunity_v4_live_enabled": True,
            "opportunity_v4_strategy_version": "v4.1",
            "performance_guard_peak_drawdown_pct": 12,
            "strategy_canary_enabled": True,
            "strategy_canary_auto_issue": True,
            "strategy_canary_release_id": "extreme_v4_roll@v4.1",
            "strategy_canary_level_1_multiplier": 0.4,
            "strategy_canary_level_1_max_opportunities": 3,
        },
        80,
        now=now + timedelta(minutes=5),
    )

    assert status["peak_drawdown_severe"] is True
    assert status["pause_until"] is None
    assert status["status"] == "strategy_canary_1"
    assert status["allowed"] is True
    assert status["risk_multiplier"] == 0.4
    assert status["strategy_canary_permit"]["allowed"] is True


def test_v4_guard_uses_decision_shadows_and_excludes_exploration(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    _seed_shadow(
        "GOODUSDT",
        "SHORT",
        [0.1] * 20,
        "v3_breakout",
        version="v4.0",
        role="active",
        family="extreme_v4_roll",
        evidence_type="decision",
    )
    _seed_shadow(
        "RESEARCHUSDT",
        "LONG",
        [-0.2] * 100,
        "v3_pullback",
        version="v4.0",
        role="active",
        family="extreme_v4_roll",
        evidence_type="exploration",
    )
    clear_performance_cache()

    status = global_performance_guard(
        {
            "opportunity_v4_live_enabled": True,
            "opportunity_v4_strategy_version": "v4.0",
            "performance_guard_current_release_only": True,
        },
        25,
    )

    assert status["shadow_evidence_scope"] == "extreme_v4_roll@v4.0:live_lanes"
    assert status["shadow"]["trades"] == 20
    assert status["shadow"]["net_pnl"] > 0


def test_v431_guard_excludes_shadow_only_decisions_from_recovery(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    _seed_shadow(
        "DIAGNOSTICUSDT",
        "LONG",
        [0.5] * 30,
        version="v4.3.1",
        role="active",
        family="extreme_v4_roll",
        admission_lane="shadow_only",
    )
    _seed_shadow(
        "ELIGIBLEUSDT",
        "LONG",
        [-0.1] * 8,
        version="v4.3.1",
        role="active",
        family="extreme_v4_roll",
        admission_lane="core_canary",
    )
    clear_performance_cache()

    status = global_performance_guard(
        {
            "opportunity_v4_live_enabled": True,
            "opportunity_v4_strategy_version": "v4.3.1",
            "performance_guard_current_release_only": True,
        },
        25,
    )

    assert status["shadow_evidence_scope"] == "extreme_v4_roll@v4.3.1:live_lanes"
    assert status["shadow"]["trades"] == 8
    assert status["shadow"]["net_pnl"] < 0


def test_v44_guard_counts_full_bet_decisions_and_excludes_research_shadows(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    _seed_shadow(
        "FULLBETUSDT",
        "LONG",
        [0.2] * 8,
        version="v4.4",
        role="active",
        family="extreme_v4_roll",
        admission_lane="full_bet",
    )
    _seed_shadow(
        "RESEARCHUSDT",
        "SHORT",
        [-0.5] * 20,
        version="v4.4",
        role="active",
        family="extreme_v4_roll",
        admission_lane="shadow_only",
    )
    _seed_shadow(
        "EXPLORATIONUSDT",
        "LONG",
        [1.0] * 20,
        version="v4.4",
        role="active",
        family="extreme_v4_roll",
        evidence_type="exploration",
        admission_lane="full_bet",
    )
    clear_performance_cache()

    status = global_performance_guard(
        {
            "opportunity_v4_live_enabled": True,
            "opportunity_v4_strategy_version": "v4.4",
            "performance_guard_current_release_only": True,
        },
        25,
    )

    assert status["shadow_evidence_scope"] == "extreme_v4_roll@v4.4:live_lanes"
    assert status["shadow"]["trades"] == 8
    assert status["shadow"]["net_pnl"] == 1.6
    assert status["recovery_requirements"]["eligible_admission_lanes"] == ["full_bet"]


def test_four_losses_use_soft_observation_before_hard_cooldown(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    _seed_live("SOFTUSDT", "LONG", [-0.1] * 4)
    clear_performance_cache()

    status = global_performance_guard({}, 25)

    assert status["status"] == "soft_observation"
    assert status["guard_level"] == "soft"
    assert status["hard_risk_off"] is False
    assert status["cooldown_minutes"] == 20
    assert status["shadow_bad"] is False


def test_five_losses_use_hard_global_cooldown(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    _seed_live("HARDUSDT", "LONG", [-0.1] * 5)
    clear_performance_cache()

    status = global_performance_guard({}, 25)

    assert status["status"] == "hard_cooldown"
    assert status["guard_level"] == "hard"
    assert status["hard_risk_off"] is True
    assert status["cooldown_minutes"] == 60


def test_release_equity_guard_latches_eight_percent_fallback_and_resets_on_new_version(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    config = {
        "opportunity_v4_live_enabled": True,
        "opportunity_v4_strategy_version": "v4.3.2",
        "opportunity_v432_release_fallback_drawdown_pct": 8.0,
    }

    first = update_release_equity_guard(config, 100)
    lower = update_release_equity_guard(config, 91)
    recovered = update_release_equity_guard(config, 99)
    reset = update_release_equity_guard({**config, "opportunity_v4_strategy_version": "v4.3.3"}, 99)

    assert first["fallback_active"] is False
    assert lower["drawdown_pct"] == 9.0
    assert lower["fallback_active"] is True
    assert recovered["fallback_active"] is True
    assert reset["fallback_active"] is False
    assert reset["peak_equity"] == 99


def test_release_sizing_fallback_does_not_create_global_risk_off(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    config = {
        "opportunity_v4_strategy_version": "v4.3.2",
        "performance_guard_current_release_only": True,
        "_strategy_release_equity_guard": {
            "release_id": "extreme_v4_roll@v4.3.2",
            "drawdown_pct": 8.5,
            "fallback_threshold_pct": 8.0,
            "fallback_active": True,
        },
    }

    status = global_performance_guard(config, 91)

    assert status["release_drawdown_fallback"] is True
    assert status["status"] == "normal"
    assert status["risk_multiplier"] == 1.0


def test_v432_startup_canary_caps_normal_trading_then_expires_cleanly(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    config = {
        "opportunity_v4_live_enabled": True,
        "opportunity_v4_strategy_version": "v4.3.2",
        "performance_guard_current_release_only": True,
        "strategy_canary_enabled": True,
        "strategy_canary_auto_issue": True,
        "strategy_canary_startup_cap_enabled": True,
        "strategy_canary_release_id": "extreme_v4_roll@v4.3.2",
        "strategy_canary_permit_hours": 24,
        "strategy_canary_level_1_multiplier": 0.70,
        "strategy_canary_level_1_max_opportunities": 5,
        "strategy_canary_max_losses": 2,
    }

    startup = global_performance_guard(config, 20, now=now)
    save_state(
        {
            "strategy_canary": {
                **startup["strategy_canary_permit"],
                "status": "revoked",
                "risk_multiplier": 0.0,
                "losses": 2,
                "reason": "canary_loss_budget_exhausted",
            }
        }
    )
    clear_performance_cache()
    revoked = global_performance_guard(config, 20, now=now + timedelta(hours=1))
    clear_performance_cache()
    after_window = global_performance_guard(config, 20, now=now + timedelta(hours=25))

    assert startup["guard_level"] == "normal"
    assert startup["status"] == "strategy_canary_1"
    assert startup["allowed"] is True
    assert startup["risk_multiplier"] == 0.70
    assert startup["strategy_canary_permit"]["max_opportunities"] == 5
    assert revoked["allowed"] is False
    assert revoked["risk_multiplier"] == 0.0
    assert revoked["status"] == "strategy_canary_revoked"
    assert revoked["recovery_requirements"]["eligible_admission_lanes"] == [
        "core_canary",
        "core_provisional",
        "limited_exploration",
        "validated",
    ]
    assert after_window["status"] == "normal"
    assert after_window["allowed"] is True
    assert after_window["risk_multiplier"] == 1.0
    assert after_window["strategy_canary_permit"]["status"] == "expired"
