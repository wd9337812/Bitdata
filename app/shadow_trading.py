from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from app.market_stream import read_snapshot
from app.performance_guard import observed_round_trip_cost_pct
from app.strategy_releases import (
    ACTIVE_ROLE,
    CHALLENGER_ROLE,
    V3_FAMILY,
    active_version,
    challenger_version,
    ensure_release_schema,
    ensure_shadow_release_columns,
    initialize_strategy_releases,
    migrate_shadow_release_metadata,
    parameter_fingerprint,
    release_id,
)
from app.telemetry import connect, now_iso


def ensure_shadow_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shadow_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dedupe_key TEXT NOT NULL UNIQUE,
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            signal_type TEXT,
            mode TEXT,
            status TEXT NOT NULL,
            blocked_reason TEXT,
            entry REAL NOT NULL,
            stop REAL NOT NULL,
            take_profit REAL NOT NULL,
            last_price REAL NOT NULL,
            high_price REAL,
            low_price REAL,
            notional REAL NOT NULL,
            gross_pnl REAL DEFAULT 0,
            estimated_cost REAL DEFAULT 0,
            net_pnl REAL DEFAULT 0,
            outcome TEXT,
            expires_at TEXT NOT NULL,
            payload TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_status ON shadow_trades(status, opened_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_symbol ON shadow_trades(symbol, opened_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_closed_at ON shadow_trades(status, closed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_symbol_direction_closed ON shadow_trades(symbol, direction, closed_at)")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(shadow_trades)").fetchall()}
    if "high_price" not in columns:
        conn.execute("ALTER TABLE shadow_trades ADD COLUMN high_price REAL")
    if "low_price" not in columns:
        conn.execute("ALTER TABLE shadow_trades ADD COLUMN low_price REAL")
    if "strategy_family" not in columns:
        conn.execute("ALTER TABLE shadow_trades ADD COLUMN strategy_family TEXT")
    if "market_regime" not in columns:
        conn.execute("ALTER TABLE shadow_trades ADD COLUMN market_regime TEXT")
    if "opportunity_score" not in columns:
        conn.execute("ALTER TABLE shadow_trades ADD COLUMN opportunity_score REAL")
    ensure_release_schema(conn)
    ensure_shadow_release_columns(conn)


def _candidate_price(candidate: dict[str, Any]) -> float:
    signal = candidate.get("signal") or {}
    ticker = candidate.get("ticker") or {}
    return float(signal.get("last_price") or ticker.get("last") or 0)


def _dedupe_key(candidate: dict[str, Any], bucket_minutes: int) -> str:
    current = datetime.now(timezone.utc)
    bucket = int(current.timestamp() // max(60, bucket_minutes * 60))
    return ":".join(
        [
            str(candidate.get("mode") or "growth"),
            str(candidate.get("strategy_family") or "legacy_mixed"),
            str(candidate.get("strategy_version") or "legacy"),
            str(candidate.get("strategy_role") or "legacy"),
            str(candidate.get("symbol") or "").upper(),
            str(candidate.get("direction") or "LONG").upper(),
            str(candidate.get("entry_type") or "watch"),
            str(bucket),
        ]
    )


def _opportunity_id(candidate: dict[str, Any], bucket_minutes: int) -> str:
    current = datetime.now(timezone.utc)
    bucket = int(current.timestamp() // max(60, bucket_minutes * 60))
    return ":".join(
        [
            str(candidate.get("symbol") or "").upper(),
            str(candidate.get("direction") or "LONG").upper(),
            str(candidate.get("entry_type") or "watch"),
            str(bucket),
        ]
    )


def _fresh_stream_item(item: dict[str, Any] | None, max_age_seconds: int) -> bool:
    if not item or not item.get("updated_at"):
        return False
    try:
        updated = datetime.fromisoformat(str(item["updated_at"]).replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - updated).total_seconds() <= max_age_seconds
    except ValueError:
        return False


def active_shadow_symbols(limit: int = 100) -> list[str]:
    with connect() as conn:
        ensure_shadow_tables(conn)
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM shadow_trades WHERE status = 'OPEN' LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    return [str(row[0]).upper() for row in rows if row[0]]


def update_shadow_trades(candidates: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, int]:
    """Maintain paper-only trades from scan data. This function never calls Binance."""
    if not config.get("shadow_trading_enabled", True):
        return {"opened": 0, "closed": 0, "active": 0}
    initialize_strategy_releases(config)
    now = datetime.now(timezone.utc)
    snapshot = read_snapshot()
    stream_max_age = int(config.get("shadow_stream_max_age_seconds", 20))
    prices = {
        str(item.get("symbol") or "").upper(): _candidate_price(item)
        for item in candidates
        if _candidate_price(item) > 0
    }
    for symbol, ticker in (snapshot.get("tickers") or {}).items():
        if _fresh_stream_item(ticker, stream_max_age):
            price = float(ticker.get("lastPrice") or 0)
            if price > 0:
                prices[str(symbol).upper()] = price
    cost_pct = max(
        float(config.get("shadow_round_trip_cost_pct", 0.12)),
        observed_round_trip_cost_pct(config),
    ) / 100
    opened = 0
    closed = 0
    with connect() as conn:
        ensure_shadow_tables(conn)
        migrate_shadow_release_metadata(conn, config)
        active_rows = conn.execute("SELECT * FROM shadow_trades WHERE status = 'OPEN'").fetchall()
        active_keys = {
            (
                str(row["strategy_family"] or "legacy_mixed"),
                str(row["strategy_version"] or "legacy"),
                str(row["strategy_role"] or "legacy"),
                str(row["symbol"]).upper(),
                str(row["direction"]).upper(),
                str(row["signal_type"] or "watch"),
            )
            for row in active_rows
        }
        for row in active_rows:
            item = dict(row)
            price = prices.get(str(item["symbol"]).upper())
            if not price:
                continue
            direction = str(item["direction"]).upper()
            kline = (((snapshot.get("klines") or {}).get(str(item["symbol"]).upper()) or {}).get("1m"))
            row = kline.get("row") if _fresh_stream_item(kline, stream_max_age) else None
            stream_high = float(row[2]) if row and len(row) > 3 else price
            stream_low = float(row[3]) if row and len(row) > 3 else price
            high = max(float(item.get("high_price") or item["entry"]), price, stream_high)
            low = min(float(item.get("low_price") or item["entry"]), price, stream_low)
            stop_hit = low <= float(item["stop"]) if direction == "LONG" else high >= float(item["stop"])
            take_hit = high >= float(item["take_profit"]) if direction == "LONG" else low <= float(item["take_profit"])
            expired = now >= datetime.fromisoformat(str(item["expires_at"]))
            if not (stop_hit or take_hit or expired):
                conn.execute(
                    "UPDATE shadow_trades SET last_price = ?, high_price = ?, low_price = ? WHERE id = ?",
                    (price, high, low, item["id"]),
                )
                continue
            # When both levels were crossed between observations, assume the stop happened first.
            exit_price = float(item["stop"]) if stop_hit else float(item["take_profit"]) if take_hit else price
            move = (exit_price - float(item["entry"])) / float(item["entry"])
            if direction == "SHORT":
                move = -move
            gross = float(item["notional"]) * move
            cost = float(item["estimated_cost"])
            outcome = "STOP" if stop_hit else "TAKE_PROFIT" if take_hit else "TIME_EXIT"
            conn.execute(
                """
                UPDATE shadow_trades
                SET status = 'CLOSED', closed_at = ?, last_price = ?, high_price = ?, low_price = ?, gross_pnl = ?, net_pnl = ?, outcome = ?
                WHERE id = ?
                """,
                (now_iso(), exit_price, high, low, gross, gross - cost, outcome, item["id"]),
            )
            active_keys.discard(
                (
                    str(item.get("strategy_family") or "legacy_mixed"),
                    str(item.get("strategy_version") or "legacy"),
                    str(item.get("strategy_role") or "legacy"),
                    str(item["symbol"]).upper(),
                    direction,
                    str(item.get("signal_type") or "watch"),
                )
            )
            closed += 1

        min_score = float(config.get("shadow_min_candidate_score", 70.0))
        bucket_minutes = int(config.get("shadow_dedupe_minutes", 10))
        hold_minutes = int(config.get("shadow_max_hold_minutes", 120))
        notional = float(config.get("shadow_reference_notional_usdt", 20.0))
        for candidate in candidates:
            signal = candidate.get("signal") or {}
            direction = str(candidate.get("direction") or "LONG").upper()
            v3 = candidate.get("opportunity_v3") or {}
            v33 = candidate.get("opportunity_v33") or {}
            strategy_version = str(candidate.get("strategy_version") or "legacy")
            strategy_role = str(candidate.get("strategy_role") or "legacy")
            is_v3 = candidate.get("strategy_family") == V3_FAMILY and strategy_role == ACTIVE_ROLE
            is_v33 = candidate.get("strategy_family") == V3_FAMILY and strategy_role == CHALLENGER_ROLE
            v31 = candidate.get("opportunity_v31") or candidate.get("v31_challenger") or {}
            is_v31 = candidate.get("strategy_family") == "extreme_v31_challenger"
            entry = _candidate_price(candidate)
            stop = float(signal.get("stop") or 0)
            take = float(signal.get("take_profit") or 0)
            if (
                float(candidate.get("score") or 0)
                < (
                    float(config.get("opportunity_v33_min_score", 68.0))
                    if is_v33
                    else float(config.get("opportunity_v31_shadow_min_score", 68.0))
                    if is_v31
                    else min_score
                )
                or (is_v3 and signal.get("signal") != direction)
                or (is_v3 and not v3.get("eligible"))
                or (is_v33 and signal.get("signal") != direction)
                or (is_v33 and not v33.get("eligible"))
                or (is_v31 and signal.get("signal") != direction)
                or (is_v31 and not v31.get("eligible"))
                or entry <= 0
                or stop <= 0
                or take <= 0
                or stop == take
            ):
                continue
            key = _dedupe_key(candidate, bucket_minutes)
            active_key = (
                str(candidate.get("strategy_family") or "legacy_mixed"),
                strategy_version,
                strategy_role,
                str(candidate.get("symbol") or "").upper(),
                str(candidate.get("direction") or "LONG").upper(),
                str(candidate.get("entry_type") or "watch"),
            )
            if active_key in active_keys:
                continue
            try:
                strategy_family = str(candidate.get("strategy_family") or "legacy_mixed")
                market_regime = str((candidate.get("market_state") or {}).get("state") or "unknown")
                candidate_hold_minutes = hold_minutes
                if strategy_family in {V3_FAMILY, "extreme_v31_challenger"}:
                    protection = signal.get("protection_profile") or {}
                    candidate_hold_minutes = min(
                        hold_minutes,
                        max(5, int(protection.get("max_hold_bars") or 12) * 5),
                    )
                conn.execute(
                    """
                    INSERT INTO shadow_trades (
                        dedupe_key, opened_at, symbol, direction, signal_type, mode, status,
                        blocked_reason, entry, stop, take_profit, last_price, high_price, low_price, notional,
                        estimated_cost, expires_at, strategy_family, strategy_version, strategy_role,
                        release_id, opportunity_id, parameter_fingerprint, feature_schema_version,
                        market_regime, opportunity_score, payload
                    ) VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        now_iso(),
                        str(candidate.get("symbol") or "").upper(),
                        direction,
                        str(candidate.get("entry_type") or "watch"),
                        str(candidate.get("mode") or "growth"),
                        str(candidate.get("decision_reason") or candidate.get("reason") or "未通过实盘条件"),
                        entry,
                        stop,
                        take,
                        entry,
                        entry,
                        entry,
                        notional,
                        notional * cost_pct,
                        (now + timedelta(minutes=candidate_hold_minutes)).isoformat(),
                        strategy_family,
                        strategy_version,
                        strategy_role,
                        release_id(strategy_family, strategy_version),
                        str(candidate.get("opportunity_id") or _opportunity_id(candidate, bucket_minutes)),
                        str(candidate.get("parameter_fingerprint") or parameter_fingerprint(config, strategy_role)),
                        "v2",
                        market_regime,
                        float((v33 if is_v33 else v31 if is_v31 else v3).get("score") or candidate.get("score") or 0),
                        json.dumps(
                            {
                                "score": candidate.get("score"),
                                "market_state": candidate.get("market_state"),
                                "adaptive_thresholds": candidate.get("adaptive_thresholds"),
                                "cost_ratio": candidate.get("cost_ratio"),
                                "passed": candidate.get("passed"),
                                "strategy_version": candidate.get("strategy_version"),
                                "strategy_role": strategy_role,
                                "tier": candidate.get("v3_tier") or v3.get("tier"),
                                "entry_type": candidate.get("entry_type"),
                                "features": {
                                    "spread_pct": (candidate.get("depth") or {}).get("spread_pct"),
                                    "depth_notional": (candidate.get("depth") or {}).get("depth_notional"),
                                    "volume_acceleration": signal.get("volume_acceleration"),
                                    "directed_trade_flow": signal.get("directed_trade_flow"),
                                    "impulse_atr": signal.get("impulse_atr"),
                                    "breakout_extension_atr": signal.get("breakout_extension_atr"),
                                    "entry_phase": signal.get("entry_phase"),
                                    "medium_trend_aligned": (v33 if is_v33 else v3).get("medium_trend_aligned"),
                                    "medium_path_efficiency": (v33 if is_v33 else v3).get("medium_path_efficiency"),
                                },
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    ),
                )
                active_keys.add(active_key)
                opened += 1
            except sqlite3.IntegrityError:
                pass
        conn.commit()
        active = conn.execute("SELECT COUNT(*) FROM shadow_trades WHERE status = 'OPEN'").fetchone()[0]
    return {"opened": opened, "closed": closed, "active": int(active)}


def _shadow_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    closed_rows = [row for row in rows if str(row.get("status")) == "CLOSED"]
    positive = sum(float(row.get("net_pnl") or 0) for row in closed_rows if float(row.get("net_pnl") or 0) > 0)
    negative = sum(float(row.get("net_pnl") or 0) for row in closed_rows if float(row.get("net_pnl") or 0) < 0)
    opened_values = [str(row.get("opened_at")) for row in closed_rows if row.get("opened_at")]
    closed_values = [str(row.get("closed_at")) for row in closed_rows if row.get("closed_at")]
    span_hours = 0.0
    if opened_values and closed_values:
        try:
            span_hours = max(
                0.0,
                (datetime.fromisoformat(max(closed_values)) - datetime.fromisoformat(min(opened_values))).total_seconds() / 3600,
            )
        except ValueError:
            span_hours = 0.0
    return {
        "total": len(rows),
        "active": sum(str(row.get("status")) == "OPEN" for row in rows),
        "closed": len(closed_rows),
        "wins": sum(float(row.get("net_pnl") or 0) > 0 for row in closed_rows),
        "win_rate": round(
            sum(float(row.get("net_pnl") or 0) > 0 for row in closed_rows) / len(closed_rows) * 100,
            2,
        )
        if closed_rows
        else 0.0,
        "net_pnl": round(positive + negative, 8),
        "cost": round(sum(float(row.get("estimated_cost") or 0) for row in closed_rows), 8),
        "profit_factor": round(positive / abs(negative), 4) if negative < 0 else (999.0 if positive > 0 else 0.0),
        "regimes": len({str(row.get("market_regime") or "unknown") for row in closed_rows}),
        "symbols": len({str(row.get("symbol") or "") for row in closed_rows}),
        "span_hours": round(span_hours, 2),
    }


def shadow_summary(limit: int = 100, config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    initialize_strategy_releases(config)
    current_version = active_version(config)
    candidate_version = challenger_version(config)
    with connect() as conn:
        ensure_shadow_tables(conn)
        migrate_shadow_release_metadata(conn, config)
        rows = conn.execute("SELECT * FROM shadow_trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        aggregate = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END) AS closed,
                SUM(CASE WHEN status = 'CLOSED' AND net_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN status = 'CLOSED' THEN net_pnl ELSE 0 END) AS net_pnl,
                SUM(CASE WHEN status = 'CLOSED' THEN estimated_cost ELSE 0 END) AS cost
            FROM shadow_trades
            """
        ).fetchone()
        strategy_rows = conn.execute(
            """
            SELECT
                COALESCE(strategy_family, 'legacy_mixed') AS strategy_family,
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END) AS closed,
                SUM(CASE WHEN status = 'CLOSED' AND net_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN status = 'CLOSED' THEN net_pnl ELSE 0 END) AS net_pnl,
                SUM(CASE WHEN status = 'CLOSED' THEN estimated_cost ELSE 0 END) AS cost,
                SUM(CASE WHEN status = 'CLOSED' AND net_pnl > 0 THEN net_pnl ELSE 0 END) AS gross_wins,
                -SUM(CASE WHEN status = 'CLOSED' AND net_pnl < 0 THEN net_pnl ELSE 0 END) AS gross_losses
            FROM shadow_trades
            GROUP BY COALESCE(strategy_family, 'legacy_mixed')
            """
        ).fetchall()
        release_rows = conn.execute(
            """
            SELECT
                COALESCE(strategy_family, 'legacy_mixed') AS strategy_family,
                COALESCE(strategy_version, 'legacy') AS strategy_version,
                COALESCE(strategy_role, 'legacy') AS strategy_role,
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END) AS closed,
                SUM(CASE WHEN status = 'CLOSED' AND net_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN status = 'CLOSED' THEN net_pnl ELSE 0 END) AS net_pnl,
                SUM(CASE WHEN status = 'CLOSED' THEN estimated_cost ELSE 0 END) AS cost,
                SUM(CASE WHEN status = 'CLOSED' AND net_pnl > 0 THEN net_pnl ELSE 0 END) AS gross_wins,
                -SUM(CASE WHEN status = 'CLOSED' AND net_pnl < 0 THEN net_pnl ELSE 0 END) AS gross_losses
            FROM shadow_trades
            GROUP BY strategy_family, strategy_version, strategy_role
            ORDER BY MAX(id) DESC
            """
        ).fetchall()
        active_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM shadow_trades WHERE strategy_family = ? AND strategy_version = ? "
                "AND strategy_role = 'active' ORDER BY id DESC LIMIT ?",
                (V3_FAMILY, current_version, max(500, int(config.get("opportunity_v3_calibration_max_shadow_trades", 1500)))),
            ).fetchall()
        ]
        candidate_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM shadow_trades WHERE strategy_family = ? AND strategy_version = ? "
                "AND strategy_role = 'challenger' ORDER BY id DESC LIMIT ?",
                (V3_FAMILY, candidate_version, max(500, int(config.get("opportunity_v33_validation_min_trades", 300)) * 3)),
            ).fetchall()
        ]
    stats = dict(aggregate) if aggregate else {}
    for key in ["total", "active", "closed", "wins", "net_pnl", "cost"]:
        stats[key] = stats.get(key) or 0
    closed = int(stats.get("closed") or 0)
    wins = int(stats.get("wins") or 0)
    stats["win_rate"] = wins / closed * 100 if closed else 0.0
    by_strategy = []
    for row in strategy_rows:
        item = dict(row)
        item_closed = int(item.get("closed") or 0)
        item_wins = int(item.get("wins") or 0)
        gross_wins = float(item.get("gross_wins") or 0)
        gross_losses = float(item.get("gross_losses") or 0)
        item["win_rate"] = item_wins / item_closed * 100 if item_closed else 0.0
        item["profit_factor"] = gross_wins / gross_losses if gross_losses else (999.0 if gross_wins > 0 else 0.0)
        by_strategy.append(item)
    by_release = []
    for row in release_rows:
        item = dict(row)
        item_closed = int(item.get("closed") or 0)
        item_wins = int(item.get("wins") or 0)
        gross_wins = float(item.get("gross_wins") or 0)
        gross_losses = float(item.get("gross_losses") or 0)
        item["win_rate"] = item_wins / item_closed * 100 if item_closed else 0.0
        item["profit_factor"] = gross_wins / gross_losses if gross_losses else (999.0 if gross_wins > 0 else 0.0)
        item["release_id"] = release_id(str(item["strategy_family"]), str(item["strategy_version"]))
        by_release.append(item)
    trades = []
    for row in rows:
        item = dict(row)
        try:
            item["payload"] = json.loads(item.get("payload") or "{}")
        except json.JSONDecodeError:
            item["payload"] = {}
        trades.append(item)
    active_closed = [row for row in active_rows if str(row.get("status")) == "CLOSED"]
    candidate_closed = [row for row in candidate_rows if str(row.get("status")) == "CLOSED"]
    shadow_window = int(config.get("performance_guard_shadow_window_trades", 100))
    recovery_window = int(config.get("performance_guard_recovery_shadow_trades", 20))
    candidate_stats = _shadow_stats(candidate_rows)
    min_trades = int(config.get("opportunity_v33_validation_min_trades", 300))
    min_hours = float(config.get("opportunity_v33_validation_min_hours", 72))
    min_pf = float(config.get("opportunity_v33_validation_min_profit_factor", 1.15))
    min_regimes = int(config.get("opportunity_v33_validation_min_regimes", 2))
    candidate_validation = {
        "min_trades": min_trades,
        "min_hours": min_hours,
        "min_profit_factor": min_pf,
        "min_regimes": min_regimes,
        "trades_ready": candidate_stats["closed"] >= min_trades,
        "hours_ready": candidate_stats["span_hours"] >= min_hours,
        "profit_factor_ready": candidate_stats["profit_factor"] >= min_pf and candidate_stats["net_pnl"] > 0,
        "regimes_ready": candidate_stats["regimes"] >= min_regimes,
    }
    candidate_validation["ready_for_review"] = all(
        candidate_validation[key]
        for key in ("trades_ready", "hours_ready", "profit_factor_ready", "regimes_ready")
    )
    return {
        "stats": stats,
        "by_strategy": by_strategy,
        "by_release": by_release,
        "active_release": {
            "strategy_family": V3_FAMILY,
            "strategy_version": current_version,
            "strategy_role": ACTIVE_ROLE,
            "all": _shadow_stats(active_rows),
            "recent": _shadow_stats(active_closed[:shadow_window]),
            "recovery": _shadow_stats(active_closed[:recovery_window]),
        },
        "challenger_release": {
            "strategy_family": V3_FAMILY,
            "strategy_version": candidate_version,
            "strategy_role": CHALLENGER_ROLE,
            "all": candidate_stats,
            "recent": _shadow_stats(candidate_closed[:shadow_window]),
            "validation": candidate_validation,
        },
        "trades": trades,
    }
