from __future__ import annotations

import json
from datetime import datetime, timezone

from app.live_learning import init_live_learning_schema, rebuild_symbol_scores
from app.shadow_trading import ensure_shadow_tables
from app.strategy_releases import list_strategy_releases, migrate_shadow_release_metadata
from app.telemetry import connect


def test_release_registry_and_legacy_migration(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    config = {
        "opportunity_v3_strategy_version": "v3.2",
        "opportunity_v33_strategy_version": "v3.3-candidate",
    }
    with connect() as conn:
        ensure_shadow_tables(conn)
        conn.execute(
            """
            INSERT INTO shadow_trades (
                dedupe_key, opened_at, symbol, direction, status, entry, stop, take_profit,
                last_price, notional, expires_at, strategy_family, payload
            ) VALUES ('legacy-v31', '2026-01-01T00:00:00+00:00', 'ALTUSDT', 'LONG',
                      'OPEN', 1, 0.9, 1.1, 1, 20, '2026-01-02T00:00:00+00:00',
                      'extreme_v31_challenger', ?)
            """,
            (json.dumps({"strategy_version": "v3.1"}),),
        )
        migrated = migrate_shadow_release_metadata(conn, config)
        row = conn.execute("SELECT strategy_version, strategy_role, release_id FROM shadow_trades").fetchone()
        conn.commit()

    releases = list_strategy_releases(config)
    roles = {(row["strategy_version"], row["role"]) for row in releases}

    assert migrated > 0
    assert row["strategy_version"] == "v3.1"
    assert row["strategy_role"] == "archived"
    assert row["release_id"] == "extreme_v31_challenger@v3.1"
    assert ("v3.2", "active") in roles
    assert ("v3.3-candidate", "archived") in roles
    assert ("v4.3", "challenger") in roles
    assert ("v3.1-legacy", "archived") in roles


def test_v4_live_release_retires_v3(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    config = {
        "opportunity_v3_strategy_version": "v3.2",
        "opportunity_v4_strategy_version": "v4.0",
        "opportunity_v4_live_enabled": True,
    }

    releases = list_strategy_releases(config)
    roles = {(row["strategy_family"], row["strategy_version"], row["role"], row["status"]) for row in releases}

    assert ("extreme_v4_roll", "v4.0", "active", "live") in roles
    assert ("extreme_v3_roll", "v3.2", "archived", "retired") in roles


def test_new_v4_release_archives_older_v4_release(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    list_strategy_releases(
        {
            "opportunity_v3_strategy_version": "v3.2",
            "opportunity_v4_strategy_version": "v4.1",
            "opportunity_v4_live_enabled": True,
        }
    )
    releases = list_strategy_releases(
        {
            "opportunity_v3_strategy_version": "v3.2",
            "opportunity_v4_strategy_version": "v4.2",
            "opportunity_v4_live_enabled": True,
        }
    )
    roles = {(row["strategy_version"], row["role"], row["status"]) for row in releases}

    assert ("v4.2", "active", "live") in roles
    assert ("v4.1", "archived", "retired") in roles


def test_live_legacy_placeholder_is_backfilled_from_exact_open_decision(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    init_live_learning_schema()
    opened = datetime.now(timezone.utc).replace(microsecond=0)
    opened_ms = int(opened.timestamp() * 1000)
    payload = {
        "decision": {
            "candidate": {
                "strategy_family": "extreme_v3_roll",
                "strategy_version": "v3.2",
                "strategy_role": "active",
            }
        }
    }
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO live_trade_records (
                symbol, direction, open_time, close_time, open_notional, realized_pnl,
                commission, funding_fee, net_pnl, hold_seconds, strategy_family,
                strategy_version, strategy_role, release_id, created_at
            ) VALUES ('ALTUSDT', 'LONG', ?, ?, 20, 1, 0.02, 0, 0.98, 60,
                      'extreme_v3_roll', 'legacy', 'legacy',
                      'extreme_v3_roll@legacy', ?)
            """,
            (opened_ms, opened_ms + 60_000, opened.isoformat()),
        )
        conn.execute(
            "INSERT INTO strategy_runs (ts, symbol, action, payload) VALUES (?, 'ALTUSDT', 'OPEN_LONG', ?)",
            (opened.isoformat(), json.dumps(payload)),
        )
        conn.commit()

    rebuild_symbol_scores({"strategy_family_credit_enabled": True})

    with connect() as conn:
        row = conn.execute(
            "SELECT strategy_family, strategy_version, strategy_role, release_id FROM live_trade_records"
        ).fetchone()
    assert row["strategy_family"] == "extreme_v3_roll"
    assert row["strategy_version"] == "v3.2"
    assert row["strategy_role"] == "active"
    assert row["release_id"] == "extreme_v3_roll@v3.2"
