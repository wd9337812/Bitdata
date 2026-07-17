from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from typing import Any

from app.telemetry import connect, now_iso


V3_FAMILY = "extreme_v3_roll"
V31_ARCHIVE_FAMILY = "extreme_v31_challenger"
V4_FAMILY = "extreme_v4_roll"
ACTIVE_ROLE = "active"
CHALLENGER_ROLE = "challenger"
ARCHIVED_ROLE = "archived"
LEGACY_ROLE = "legacy"

_ACTIVE_FINGERPRINT_KEYS = (
    "opportunity_v3_a_plus_score",
    "opportunity_v3_a_score",
    "opportunity_v3_b_score",
    "opportunity_v3_a_plus_min_cost_ratio",
    "opportunity_v3_a_min_cost_ratio",
    "opportunity_v3_b_min_cost_ratio",
    "opportunity_v3_long_strength_floor",
    "opportunity_v3_short_strength_floor",
    "opportunity_v3_max_spread_pct",
    "opportunity_v3_min_depth_notional_usdt",
    "opportunity_v3_max_breakout_extension_atr",
    "opportunity_v3_max_entry_impulse_atr",
    "opportunity_v3_min_medium_path_efficiency",
)

_CHALLENGER_FINGERPRINT_KEYS = _ACTIVE_FINGERPRINT_KEYS + (
    "opportunity_v4_decision_min_rank_percentile",
    "opportunity_v4_admission_min_trades",
    "opportunity_v4_admission_min_profit_factor",
    "opportunity_v4_admission_min_lower_expectancy_pct",
    "opportunity_v4_evidence_lookback_hours",
    "opportunity_v41_min_expected_net_pct",
    "opportunity_v41_min_cost_ratio",
    "opportunity_v41_medium_alignment_required",
    "opportunity_v41_validation_min_trades",
    "opportunity_v41_validation_min_profit_factor",
    "opportunity_v41_provisional_risk_multiplier",
    "opportunity_v41_retest_stop_atr",
    "opportunity_v41_retest_take_profit_atr",
    "opportunity_v41_armed_stop_atr",
    "opportunity_v41_armed_take_profit_atr",
    "opportunity_v41_triggered_stop_atr",
    "opportunity_v41_triggered_take_profit_atr",
)


def active_version(config: dict[str, Any]) -> str:
    return str(config.get("opportunity_v3_strategy_version") or "v3.2")


def challenger_version(config: dict[str, Any]) -> str:
    return str(config.get("opportunity_v4_strategy_version") or "v4.1")


def active_family(config: dict[str, Any]) -> str:
    return V4_FAMILY if config.get("opportunity_v4_live_enabled", False) else V3_FAMILY


def active_release_version(config: dict[str, Any]) -> str:
    return challenger_version(config) if active_family(config) == V4_FAMILY else active_version(config)


def release_id(family: str, version: str) -> str:
    return f"{family}@{version}"


def parameter_fingerprint(
    config: dict[str, Any],
    role: str = ACTIVE_ROLE,
    family: str | None = None,
) -> str:
    keys = _CHALLENGER_FINGERPRINT_KEYS if role == CHALLENGER_ROLE or family == V4_FAMILY else _ACTIVE_FINGERPRINT_KEYS
    payload = {key: config.get(key) for key in keys if key in config}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _add_column(conn: sqlite3.Connection, table: str, definition: str) -> None:
    name = definition.split()[0]
    columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def ensure_release_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS strategy_releases (
            release_id TEXT PRIMARY KEY,
            strategy_family TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            role TEXT NOT NULL,
            status TEXT NOT NULL,
            parameter_fingerprint TEXT NOT NULL,
            git_commit TEXT,
            config_snapshot TEXT,
            activated_at TEXT,
            retired_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_releases_role "
        "ON strategy_releases(strategy_family, role, status)"
    )


def ensure_shadow_release_columns(conn: sqlite3.Connection) -> None:
    _add_column(conn, "shadow_trades", "strategy_version TEXT")
    _add_column(conn, "shadow_trades", "strategy_role TEXT")
    _add_column(conn, "shadow_trades", "release_id TEXT")
    _add_column(conn, "shadow_trades", "opportunity_id TEXT")
    _add_column(conn, "shadow_trades", "parameter_fingerprint TEXT")
    _add_column(conn, "shadow_trades", "feature_schema_version TEXT")
    _add_column(conn, "shadow_trades", "evidence_type TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_shadow_release_status "
        "ON shadow_trades(strategy_family, strategy_version, strategy_role, status, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_shadow_opportunity_release "
        "ON shadow_trades(opportunity_id, strategy_version, strategy_role)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_shadow_v41_evidence "
        "ON shadow_trades(strategy_family, strategy_version, evidence_type, status, closed_at DESC)"
    )


def ensure_live_release_columns(conn: sqlite3.Connection) -> None:
    _add_column(conn, "live_trade_records", "strategy_family TEXT")
    _add_column(conn, "live_trade_records", "strategy_version TEXT")
    _add_column(conn, "live_trade_records", "strategy_role TEXT")
    _add_column(conn, "live_trade_records", "release_id TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_live_trade_release_close "
        "ON live_trade_records(strategy_family, strategy_version, strategy_role, close_time DESC)"
    )


def _register(
    conn: sqlite3.Connection,
    *,
    family: str,
    version: str,
    role: str,
    status: str,
    fingerprint: str,
    config: dict[str, Any],
) -> None:
    current = now_iso()
    keys = _CHALLENGER_FINGERPRINT_KEYS if role == CHALLENGER_ROLE or family == V4_FAMILY else _ACTIVE_FINGERPRINT_KEYS
    snapshot = {key: config.get(key) for key in keys if key in config}
    conn.execute(
        """
        INSERT INTO strategy_releases (
            release_id, strategy_family, strategy_version, role, status,
            parameter_fingerprint, git_commit, config_snapshot, activated_at,
            retired_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(release_id) DO UPDATE SET
            role = excluded.role,
            status = excluded.status,
            parameter_fingerprint = excluded.parameter_fingerprint,
            git_commit = COALESCE(excluded.git_commit, strategy_releases.git_commit),
            config_snapshot = excluded.config_snapshot,
            activated_at = COALESCE(strategy_releases.activated_at, excluded.activated_at),
            retired_at = COALESCE(strategy_releases.retired_at, excluded.retired_at),
            updated_at = excluded.updated_at
        WHERE strategy_releases.role != excluded.role
           OR strategy_releases.status != excluded.status
           OR strategy_releases.parameter_fingerprint != excluded.parameter_fingerprint
           OR strategy_releases.config_snapshot != excluded.config_snapshot
           OR COALESCE(strategy_releases.git_commit, '') != COALESCE(excluded.git_commit, '')
        """,
        (
            release_id(family, version),
            family,
            version,
            role,
            status,
            fingerprint,
            os.getenv("BITDATA_GIT_COMMIT") or None,
            json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
            current if role == ACTIVE_ROLE else None,
            current if role == ARCHIVED_ROLE else None,
            current,
            current,
        ),
    )


def initialize_strategy_releases(config: dict[str, Any]) -> dict[str, Any]:
    v3_version = active_version(config)
    v4_version = challenger_version(config)
    live_family = active_family(config)
    live_version = active_release_version(config)
    v3_fingerprint = parameter_fingerprint(config, ACTIVE_ROLE, V3_FAMILY)
    v4_fingerprint = parameter_fingerprint(config, CHALLENGER_ROLE, V4_FAMILY)
    v4_live = live_family == V4_FAMILY
    with connect() as conn:
        ensure_release_schema(conn)
        _register(
            conn,
            family=V3_FAMILY,
            version=v3_version,
            role=ARCHIVED_ROLE if v4_live else ACTIVE_ROLE,
            status="retired" if v4_live else "live",
            fingerprint=v3_fingerprint,
            config=config,
        )
        _register(
            conn,
            family=V4_FAMILY,
            version=v4_version,
            role=ACTIVE_ROLE if v4_live else CHALLENGER_ROLE,
            status="live" if v4_live else "shadow",
            fingerprint=v4_fingerprint,
            config=config,
        )
        _register(
            conn,
            family=V31_ARCHIVE_FAMILY,
            version="v3.1-legacy",
            role=ARCHIVED_ROLE,
            status="retired",
            fingerprint="legacy",
            config={},
        )
        _register(
            conn,
            family=V3_FAMILY,
            version=str(config.get("opportunity_v33_strategy_version") or "v3.3-candidate"),
            role=ARCHIVED_ROLE,
            status="retired",
            fingerprint="legacy-v33",
            config={},
        )
        conn.commit()
    return {
        "active_release": release_id(live_family, live_version),
        "challenger_release": None if v4_live else release_id(V4_FAMILY, v4_version),
        "active_parameter_fingerprint": v4_fingerprint if v4_live else v3_fingerprint,
        "challenger_parameter_fingerprint": None if v4_live else v4_fingerprint,
    }


def migrate_shadow_release_metadata(conn: sqlite3.Connection, config: dict[str, Any]) -> int:
    ensure_shadow_release_columns(conn)
    active = active_version(config)
    challenger = challenger_version(config)
    active_fingerprint = parameter_fingerprint(config, ACTIVE_ROLE, V3_FAMILY)
    challenger_fingerprint = parameter_fingerprint(config, CHALLENGER_ROLE, V4_FAMILY)
    before = conn.total_changes
    conn.execute(
        """
        UPDATE shadow_trades
        SET strategy_version = COALESCE(
                NULLIF(strategy_version, ''),
                NULLIF(json_extract(payload, '$.strategy_version'), ''),
                'legacy'
            ),
            strategy_role = COALESCE(
                NULLIF(strategy_role, ''),
                CASE
                    WHEN strategy_family = ? THEN 'archived'
                    WHEN strategy_family = ? AND json_extract(payload, '$.strategy_version') = ? THEN 'active'
                    WHEN strategy_family = ? AND json_extract(payload, '$.strategy_version') = ? THEN 'challenger'
                    ELSE 'legacy'
                END
            )
        WHERE strategy_version IS NULL OR strategy_version = ''
           OR strategy_role IS NULL OR strategy_role = ''
        """,
        (V31_ARCHIVE_FAMILY, V3_FAMILY, active, V4_FAMILY, challenger),
    )
    conn.execute(
        """
        UPDATE shadow_trades
        SET release_id = COALESCE(NULLIF(release_id, ''), COALESCE(strategy_family, 'legacy_mixed') || '@' || strategy_version),
            parameter_fingerprint = CASE
                WHEN strategy_family = ? AND strategy_version = ? THEN COALESCE(NULLIF(parameter_fingerprint, ''), ?)
                WHEN strategy_family = ? AND strategy_version = ? THEN COALESCE(NULLIF(parameter_fingerprint, ''), ?)
                ELSE COALESCE(NULLIF(parameter_fingerprint, ''), 'legacy')
            END,
            feature_schema_version = COALESCE(NULLIF(feature_schema_version, ''), 'v1')
        WHERE release_id IS NULL OR release_id = ''
           OR parameter_fingerprint IS NULL OR parameter_fingerprint = ''
           OR feature_schema_version IS NULL OR feature_schema_version = ''
        """,
        (V3_FAMILY, active, active_fingerprint, V4_FAMILY, challenger, challenger_fingerprint),
    )
    return conn.total_changes - before


def list_strategy_releases(config: dict[str, Any]) -> list[dict[str, Any]]:
    initialize_strategy_releases(config)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM strategy_releases ORDER BY "
            "CASE role WHEN 'active' THEN 0 WHEN 'challenger' THEN 1 WHEN 'archived' THEN 2 ELSE 3 END, updated_at DESC"
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["config_snapshot"] = json.loads(item.get("config_snapshot") or "{}")
        except json.JSONDecodeError:
            item["config_snapshot"] = {}
        result.append(item)
    return result
