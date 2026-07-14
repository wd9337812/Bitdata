from __future__ import annotations

import json

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
    assert ("v3.3-candidate", "challenger") in roles
    assert ("v3.1-legacy", "archived") in roles
