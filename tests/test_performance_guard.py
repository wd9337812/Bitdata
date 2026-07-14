from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.live_learning import init_live_learning_schema
from app.performance_guard import (
    apply_strategy_evidence_to_candidate,
    clear_performance_cache,
    global_performance_guard,
    observed_round_trip_cost_pct,
    strategy_evidence_for,
)
from app.shadow_trading import ensure_shadow_tables
from app.telemetry import connect


def _seed_live(
    symbol: str,
    direction: str,
    values: list[float],
    *,
    commission: float = 0.01,
    version: str | None = None,
    role: str | None = None,
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
                    "UPDATE live_trade_records SET strategy_family = 'extreme_v3_roll', strategy_version = ?, "
                    "strategy_role = ?, release_id = ? WHERE id = last_insert_rowid()",
                    (version, role or "active", f"extreme_v3_roll@{version}"),
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
) -> None:
    now = datetime.now(timezone.utc)
    with connect() as conn:
        for index, net in enumerate(values):
            opened = now - timedelta(minutes=len(values) - index + 1)
            closed = opened + timedelta(minutes=1)
            conn.execute(
                """
                INSERT INTO shadow_trades (
                    dedupe_key, opened_at, closed_at, symbol, direction, signal_type, mode,
                    status, entry, stop, take_profit, last_price, notional, estimated_cost,
                    gross_pnl, net_pnl, outcome, expires_at, payload
                ) VALUES (?, ?, ?, ?, ?, ?, 'extreme_sprint', 'CLOSED', 1, 0.99, 1.01, 1,
                          20, 0.024, ?, ?, 'TIME_EXIT', ?, '{}')
                """,
                (
                    f"{symbol}:{direction}:{index}:{now.timestamp()}",
                    opened.isoformat(),
                    closed.isoformat(),
                    symbol,
                    direction,
                    signal,
                    net + 0.024,
                    net,
                    closed.isoformat(),
                ),
            )
            if version:
                conn.execute(
                    "UPDATE shadow_trades SET strategy_family = 'extreme_v3_roll', strategy_version = ?, "
                    "strategy_role = ?, release_id = ? WHERE dedupe_key = ?",
                    (version, role or "active", f"extreme_v3_roll@{version}", f"{symbol}:{direction}:{index}:{now.timestamp()}"),
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

    assert status["status"] == "cooldown"
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


def test_severe_live_loss_pauses_even_when_shadow_is_not_bad(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    _seed_live("BADUSDT", "SHORT", [-0.4] * 9 + [0.01])
    _seed_shadow("GOODUSDT", "LONG", [0.1] * 50)
    clear_performance_cache()

    status = global_performance_guard({}, 20)

    assert status["live_severe"] is True
    assert status["shadow_bad"] is False
    assert status["allowed"] is False
    assert "影子交易不得否决" in status["reason"]


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
