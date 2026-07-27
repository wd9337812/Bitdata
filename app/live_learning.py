from __future__ import annotations

import json
import math
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.binance_client import BinanceFuturesClient
from app.strategy_releases import ACTIVE_ROLE, ensure_live_release_columns, release_id
from app.telemetry import connect, now_iso, record_event
from app.training_lineage import (
    ensure_live_lineage_columns,
    finalize_trade_lineage,
    match_trade_record,
)


DEFAULT_SCORE = 50.0
ORDERBOOK_SCALP_ENTRY_TYPES = {"orderbook_impact", "volume_scalp", "imbalance_probe"}
ORDERBOOK_SCALP_FAMILY = "orderbook_scalp"
EXTREME_V2_FAMILY = "extreme_v2_roll"
EXTREME_V3_FAMILY = "extreme_v3_roll"
EXTREME_V4_FAMILY = "extreme_v4_roll"
GRID_STRATEGY_FAMILY = "grid_stable"
LEGACY_STRATEGY_FAMILY = "legacy_mixed"


def _is_yolo_orderbook_scalp_candidate(candidate: dict[str, Any], config: dict[str, Any]) -> bool:
    return (
        bool(config.get("yolo_scalp_credit_experiment_enabled", True))
        and str(candidate.get("mode") or "") == "yolo_scalp"
        and str(candidate.get("entry_type") or "") in ORDERBOOK_SCALP_ENTRY_TYPES
    )


def _strategy_family_for_candidate(candidate: dict[str, Any], config: dict[str, Any]) -> str | None:
    if not config.get("strategy_family_credit_enabled", True):
        return None
    explicit = str(candidate.get("strategy_family") or "")
    if explicit in {ORDERBOOK_SCALP_FAMILY, EXTREME_V2_FAMILY, EXTREME_V3_FAMILY, EXTREME_V4_FAMILY, GRID_STRATEGY_FAMILY}:
        return explicit
    if str(candidate.get("strategy_generation") or "").lower() == "v3":
        return EXTREME_V3_FAMILY
    mode = str(candidate.get("mode") or "")
    if _is_yolo_orderbook_scalp_candidate(candidate, config):
        return ORDERBOOK_SCALP_FAMILY
    if mode == "extreme_sprint":
        return EXTREME_V2_FAMILY
    if mode == "grid":
        return GRID_STRATEGY_FAMILY
    return None


def _experiment_credit(
    symbol: str,
    direction: str,
    config: dict[str, Any],
    strategy_family: str = ORDERBOOK_SCALP_FAMILY,
) -> dict[str, Any]:
    score = float(config.get("live_credit_default_score", DEFAULT_SCORE))
    labels = {
        ORDERBOOK_SCALP_FAMILY: "剥头皮新策略观察",
        EXTREME_V2_FAMILY: "极限 V2 新策略观察",
        EXTREME_V3_FAMILY: "机会引擎 V3 新策略观察",
        EXTREME_V4_FAMILY: "机会引擎 V4 新策略观察",
        GRID_STRATEGY_FAMILY: "网格新策略观察",
    }
    return {
        "enabled": True,
        "symbol": symbol,
        "direction": direction,
        "score": score,
        "status": "new",
        "strategy_family": strategy_family,
        "status_label": labels.get(strategy_family, "新策略观察"),
        "closed_trades": 0,
        "wins": 0,
        "losses": 0,
        "consecutive_wins": 0,
        "consecutive_losses": 0,
        "penalty_until": None,
        "commission": 0.0,
        "net_pnl": 0.0,
        "profit_factor": 0.0,
        "notes": [f"{strategy_family}_credit_experiment"],
    }


def init_live_learning_schema() -> None:
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS live_trade_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                open_time INTEGER NOT NULL,
                close_time INTEGER NOT NULL,
                open_price REAL,
                close_price REAL,
                quantity REAL,
                open_notional REAL,
                close_notional REAL,
                realized_pnl REAL,
                commission REAL,
                funding_fee REAL DEFAULT 0,
                net_pnl REAL,
                hold_seconds REAL,
                trade_count INTEGER,
                source TEXT,
                payload TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(symbol, direction, open_time, close_time)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_live_trade_close_time ON live_trade_records(close_time)")
        ensure_live_release_columns(conn)
        ensure_live_lineage_columns(conn)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_live_trade_symbol_direction_close ON live_trade_records(symbol, direction, close_time)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS symbol_live_scores (
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                score REAL NOT NULL,
                status TEXT NOT NULL,
                closed_trades INTEGER NOT NULL,
                wins INTEGER NOT NULL,
                losses INTEGER NOT NULL,
                win_rate REAL NOT NULL,
                net_pnl REAL NOT NULL,
                commission REAL NOT NULL,
                funding_fee REAL NOT NULL,
                profit_factor REAL NOT NULL,
                consecutive_wins INTEGER NOT NULL,
                consecutive_losses INTEGER NOT NULL,
                avg_hold_seconds REAL NOT NULL,
                last_hold_seconds REAL DEFAULT 0,
                penalty_until TEXT,
                last_trade_time INTEGER,
                notes TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(symbol, direction)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS symbol_strategy_live_scores (
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                strategy_family TEXT NOT NULL,
                score REAL NOT NULL,
                status TEXT NOT NULL,
                closed_trades INTEGER NOT NULL,
                wins INTEGER NOT NULL,
                losses INTEGER NOT NULL,
                win_rate REAL NOT NULL,
                net_pnl REAL NOT NULL,
                commission REAL NOT NULL,
                funding_fee REAL NOT NULL,
                profit_factor REAL NOT NULL,
                consecutive_wins INTEGER NOT NULL,
                consecutive_losses INTEGER NOT NULL,
                avg_hold_seconds REAL NOT NULL,
                last_hold_seconds REAL DEFAULT 0,
                penalty_until TEXT,
                last_trade_time INTEGER,
                notes TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(symbol, direction, strategy_family)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_symbol_strategy_live_scores_family "
            "ON symbol_strategy_live_scores(strategy_family, score DESC, net_pnl DESC)"
        )
        try:
            conn.execute("ALTER TABLE symbol_live_scores ADD COLUMN last_hold_seconds REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        conn.commit()


def _status_for_score(score: float) -> str:
    if score >= 80:
        return "strong"
    if score >= 65:
        return "normal"
    if score >= 45:
        return "observe"
    if score >= 30:
        return "weak"
    return "penalty"


def recovered_score(raw_score: float, last_trade_time: int | None, config: dict[str, Any]) -> dict[str, Any]:
    cap = float(config.get("live_credit_recovery_cap", DEFAULT_SCORE))
    score = max(0.0, min(100.0, float(raw_score)))
    if not config.get("live_credit_recovery_enabled", True) or not last_trade_time or score >= cap:
        return {"score": round(score, 2), "recovery_points": 0.0, "next_recovery_at": None}

    elapsed_hours = max(0.0, (time.time() * 1000 - int(last_trade_time)) / 3_600_000)
    if score < float(config.get("live_credit_fuse_score", 2.0)):
        interval = float(config.get("live_credit_low_recovery_interval_hours", 12))
        points = float(config.get("live_credit_low_recovery_points", 2.0))
    else:
        interval = float(config.get("live_credit_recovery_interval_hours", 6))
        points = float(config.get("live_credit_recovery_points", 3.0))
    steps = math.floor(elapsed_hours / max(interval, 0.0001))
    recovery = steps * points
    adjusted = min(cap, score + recovery)
    next_at = datetime.fromtimestamp(int(last_trade_time) / 1000, timezone.utc) + timedelta(hours=(steps + 1) * interval)
    return {
        "score": round(adjusted, 2),
        "recovery_points": round(adjusted - score, 2),
        "next_recovery_at": next_at.isoformat() if adjusted < cap else None,
    }


def status_label(status: str) -> str:
    return {
        "strong": "强信任",
        "normal": "正常信任",
        "observe": "观察区",
        "weak": "弱信任",
        "penalty": "惩罚区",
    }.get(status, "未知")


def _iso_from_ms(ms: int | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def _penalty_until_for(record: dict[str, Any], consecutive_losses: int, config: dict[str, Any]) -> str | None:
    if float(record.get("net_pnl") or 0) >= 0:
        return None
    close_time = int(record.get("close_time") or 0)
    hold_seconds = float(record.get("hold_seconds") or 0)
    quick_seconds = float(config.get("live_credit_quick_stop_seconds", 60))
    if consecutive_losses >= 3:
        hours = float(config.get("live_credit_three_loss_cooldown_hours", 24))
    elif consecutive_losses >= 2:
        hours = float(config.get("live_credit_two_loss_cooldown_hours", 4))
    elif hold_seconds and hold_seconds <= quick_seconds:
        hours = float(config.get("live_credit_quick_stop_cooldown_hours", 2))
    else:
        hours = float(config.get("live_credit_loss_cooldown_minutes", 30)) / 60
    return datetime.fromtimestamp(close_time / 1000, timezone.utc).replace(tzinfo=timezone.utc) + timedelta(hours=hours)


def _time_decay_multiplier(record: dict[str, Any], config: dict[str, Any]) -> float:
    if not config.get("live_credit_time_decay_enabled", False):
        return 1.0
    close_time = int(record.get("close_time") or 0)
    if close_time <= 0:
        return 1.0
    age_hours = max(0.0, (datetime.now(timezone.utc).timestamp() - close_time / 1000) / 3600)
    half_life = max(1.0, float(config.get("live_credit_decay_half_life_hours", 12.0)))
    floor = max(0.0, min(1.0, float(config.get("live_credit_decay_floor", 0.15))))
    decay = max(floor, 0.5 ** (age_hours / half_life))
    if age_hours <= 3:
        decay *= float(config.get("live_credit_recent_3h_multiplier", 1.5))
    return decay


def score_records(records: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    records = sorted(records, key=lambda item: int(item.get("close_time") or 0))
    score = float(config.get("live_credit_default_score", DEFAULT_SCORE))
    wins = 0
    losses = 0
    consecutive_wins = 0
    consecutive_losses = 0
    gross_profit = 0.0
    gross_loss = 0.0
    net_pnl = 0.0
    commission = 0.0
    funding_fee = 0.0
    hold_sum = 0.0
    notes: list[str] = []
    penalty_until: datetime | None = None

    for record in records:
        net = float(record.get("net_pnl") or 0)
        fee = abs(float(record.get("commission") or 0))
        funding = float(record.get("funding_fee") or 0)
        hold_seconds = float(record.get("hold_seconds") or 0)
        notional = max(float(record.get("open_notional") or 0), 1.0)
        net_ratio = net / notional * 100
        net_pnl += net
        commission += fee
        funding_fee += funding
        hold_sum += hold_seconds

        if net > 0:
            wins += 1
            consecutive_wins += 1
            consecutive_losses = 0
            gross_profit += net
            delta = float(config.get("live_credit_win_reward", 4.0))
            delta += min(6.0, net * float(config.get("live_credit_net_profit_reward_per_usdt", 1.5)))
            if net >= float(config.get("live_credit_big_win_usdt", 0.7)):
                delta += 2.0
            if net >= float(config.get("live_credit_large_win_usdt", 2.0)):
                delta += 4.0
            if consecutive_wins >= 2:
                delta += 3.0
            if consecutive_wins >= 3:
                delta += 5.0
            if fee > 0 and net > 0 and fee / max(net, 0.0001) <= 0.10:
                delta += float(config.get("live_credit_low_fee_reward", 1.5))
            if hold_seconds >= float(config.get("live_credit_min_quality_hold_seconds", 90)):
                delta += 1.0
            delta *= _time_decay_multiplier(record, config)
            score += delta
            notes.append(f"盈利奖励 +{delta:.1f}")
        else:
            losses += 1
            consecutive_losses += 1
            consecutive_wins = 0
            gross_loss += abs(net)
            delta = -float(config.get("live_credit_loss_penalty", 6.0))
            if net <= -float(config.get("live_credit_big_loss_usdt", 0.7)):
                delta -= 3.0
            if net <= -float(config.get("live_credit_large_loss_usdt", 2.0)):
                delta -= 8.0
            if hold_seconds and hold_seconds <= float(config.get("live_credit_quick_stop_seconds", 60)):
                delta -= 8.0
            if consecutive_losses >= 2:
                delta -= float(config.get("live_credit_consecutive_loss_penalty", 10.0))
            if consecutive_losses >= 3:
                delta -= 15.0
            if abs(net_ratio) < 0.3 and fee > abs(net) * 0.20:
                delta -= float(config.get("live_credit_fee_drag_penalty", 3.0))
            delta *= _time_decay_multiplier(record, config)
            score += delta
            notes.append(f"亏损惩罚 {delta:.1f}")
            until = _penalty_until_for(record, consecutive_losses, config)
            if until and (penalty_until is None or until > penalty_until):
                penalty_until = until

        score = max(0.0, min(100.0, score))

    closed = len(records)
    profit_factor = gross_profit / gross_loss if gross_loss else (999.0 if gross_profit > 0 else 0.0)
    last_trade_time = int(records[-1].get("close_time") or 0) if records else None
    last_hold_seconds = float(records[-1].get("hold_seconds") or 0) if records else 0.0
    recovery = recovered_score(score, last_trade_time, config)
    score = float(recovery["score"])
    if recovery["recovery_points"] > 0:
        notes.append(f"自然恢复 +{recovery['recovery_points']:.1f}")
    status = _status_for_score(score)
    if status == "penalty" and penalty_until is None and records:
        close_time = int(records[-1].get("close_time") or 0)
        penalty_until = datetime.fromtimestamp(close_time / 1000, timezone.utc) + timedelta(
            hours=float(config.get("live_credit_penalty_cooldown_hours", 12))
        )
    return {
        "score": round(score, 2),
        "status": status,
        "status_label": status_label(status),
        "closed_trades": closed,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / closed * 100, 2) if closed else 0.0,
        "net_pnl": round(net_pnl, 8),
        "commission": round(commission, 8),
        "funding_fee": round(funding_fee, 8),
        "profit_factor": round(profit_factor, 4),
        "consecutive_wins": consecutive_wins,
        "consecutive_losses": consecutive_losses,
        "avg_hold_seconds": round(hold_sum / closed, 2) if closed else 0.0,
        "last_hold_seconds": round(last_hold_seconds, 2),
        "penalty_until": penalty_until.isoformat() if penalty_until else None,
        "last_trade_time": last_trade_time,
        "recovery_points": recovery["recovery_points"],
        "next_recovery_at": recovery["next_recovery_at"],
        "notes": notes[-6:],
    }


def upsert_trade_records(records: list[dict[str, Any]]) -> int:
    init_live_learning_schema()
    prepared_records: list[dict[str, Any]] = []
    for source in records:
        record = dict(source)
        try:
            lineage = match_trade_record(record)
            for key, value in lineage.items():
                if value is not None and record.get(key) in {None, ""}:
                    record[key] = value
            if record.get("strategy_family") and record.get("strategy_version"):
                record["release_id"] = release_id(
                    str(record["strategy_family"]),
                    str(record["strategy_version"]),
                )
            finalize_trade_lineage(record)
        except sqlite3.OperationalError:
            record.setdefault("lineage_quality", "unavailable")
        prepared_records.append(record)

    inserted = 0
    with connect() as conn:
        for record in prepared_records:
            conn.execute(
                """
                INSERT INTO live_trade_records (
                    symbol, direction, open_time, close_time, open_price, close_price,
                    quantity, open_notional, close_notional, realized_pnl, commission,
                    funding_fee, net_pnl, hold_seconds, trade_count, source, payload, created_at,
                    opportunity_id, entry_order_ids, exit_order_ids, entry_slippage_bps,
                    exit_reason, lineage_quality, strategy_family, strategy_version,
                    strategy_role, release_id, event_id
                    , event_group_id, execution_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, direction, open_time, close_time) DO UPDATE SET
                    open_price = excluded.open_price,
                    close_price = excluded.close_price,
                    quantity = excluded.quantity,
                    open_notional = excluded.open_notional,
                    close_notional = excluded.close_notional,
                    realized_pnl = excluded.realized_pnl,
                    commission = excluded.commission,
                    funding_fee = excluded.funding_fee,
                    net_pnl = excluded.net_pnl,
                    hold_seconds = excluded.hold_seconds,
                    trade_count = excluded.trade_count,
                    source = excluded.source,
                    payload = CASE
                        WHEN excluded.payload NOT IN ('', '{}') THEN excluded.payload
                        ELSE live_trade_records.payload
                    END,
                    opportunity_id = COALESCE(
                        NULLIF(excluded.opportunity_id, ''),
                        live_trade_records.opportunity_id
                    ),
                    entry_order_ids = CASE
                        WHEN excluded.entry_order_ids NOT IN ('', '[]') THEN excluded.entry_order_ids
                        ELSE live_trade_records.entry_order_ids
                    END,
                    exit_order_ids = CASE
                        WHEN excluded.exit_order_ids NOT IN ('', '[]') THEN excluded.exit_order_ids
                        ELSE live_trade_records.exit_order_ids
                    END,
                    entry_slippage_bps = COALESCE(
                        excluded.entry_slippage_bps,
                        live_trade_records.entry_slippage_bps
                    ),
                    exit_reason = COALESCE(
                        NULLIF(excluded.exit_reason, ''),
                        live_trade_records.exit_reason
                    ),
                    lineage_quality = CASE
                        WHEN excluded.lineage_quality = 'exact_order_id'
                            THEN excluded.lineage_quality
                        WHEN live_trade_records.lineage_quality IS NOT NULL
                            THEN live_trade_records.lineage_quality
                        ELSE excluded.lineage_quality
                    END,
                    strategy_family = COALESCE(
                        NULLIF(excluded.strategy_family, ''),
                        live_trade_records.strategy_family
                    ),
                    strategy_version = COALESCE(
                        NULLIF(excluded.strategy_version, ''),
                        live_trade_records.strategy_version
                    ),
                    strategy_role = COALESCE(
                        NULLIF(excluded.strategy_role, ''),
                        live_trade_records.strategy_role
                    ),
                    release_id = COALESCE(
                        NULLIF(excluded.release_id, ''),
                        live_trade_records.release_id
                    ),
                    event_id = COALESCE(
                        NULLIF(excluded.event_id, ''),
                        live_trade_records.event_id
                    ),
                    event_group_id = COALESCE(
                        NULLIF(excluded.event_group_id, ''),
                        live_trade_records.event_group_id
                    ),
                    execution_id = COALESCE(
                        NULLIF(excluded.execution_id, ''),
                        live_trade_records.execution_id
                    )
                """,
                (
                    record["symbol"],
                    record["direction"],
                    int(record["open_time"]),
                    int(record["close_time"]),
                    record.get("open_price"),
                    record.get("close_price"),
                    record.get("quantity"),
                    record.get("open_notional"),
                    record.get("close_notional"),
                    record.get("realized_pnl"),
                    record.get("commission"),
                    record.get("funding_fee", 0.0),
                    record.get("net_pnl"),
                    record.get("hold_seconds"),
                    record.get("trade_count"),
                    record.get("source", "binance"),
                    json.dumps(record.get("payload") or {}, ensure_ascii=False),
                    now_iso(),
                    record.get("opportunity_id"),
                    json.dumps(record.get("entry_order_ids") or []),
                    json.dumps(record.get("exit_order_ids") or []),
                    record.get("entry_slippage_bps"),
                    record.get("exit_reason"),
                    record.get("lineage_quality"),
                    record.get("strategy_family"),
                    record.get("strategy_version"),
                    record.get("strategy_role"),
                    record.get("release_id"),
                    record.get("event_id"),
                    record.get("event_group_id") or record.get("event_id"),
                    record.get("execution_id"),
                ),
            )
            inserted += 1
        conn.commit()
    return inserted


def rebuild_symbol_scores(config: dict[str, Any], lookback_hours: float | None = None) -> list[dict[str, Any]]:
    init_live_learning_schema()
    cutoff_ms = None
    if lookback_hours:
        cutoff_ms = int((time.time() - lookback_hours * 3600) * 1000)
    with connect() as conn:
        if cutoff_ms:
            rows = conn.execute(
                "SELECT * FROM live_trade_records WHERE close_time >= ? ORDER BY close_time ASC",
                (cutoff_ms,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM live_trade_records ORDER BY close_time ASC").fetchall()
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        grouped_strategy: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for row in rows:
            item = dict(row)
            grouped.setdefault((item["symbol"], item["direction"]), []).append(item)
            metadata = _strategy_metadata_for_record(conn, item, config)
            family = metadata["strategy_family"]
            item.update(metadata)
            expected_release = release_id(family, metadata["strategy_version"])
            if (
                str(row["strategy_family"] or "") != family
                or str(row["strategy_version"] or "") != metadata["strategy_version"]
                or str(row["strategy_role"] or "") != metadata["strategy_role"]
                or str(row["release_id"] or "") != expected_release
            ):
                conn.execute(
                    "UPDATE live_trade_records SET strategy_family = ?, strategy_version = ?, "
                    "strategy_role = ?, release_id = ? WHERE id = ?",
                    (family, metadata["strategy_version"], metadata["strategy_role"], expected_release, item["id"]),
                )
            grouped_strategy.setdefault((item["symbol"], item["direction"], family), []).append(item)
        results = []
        for (symbol, direction), records in grouped.items():
            score = score_records(records, config)
            conn.execute(
                """
                INSERT OR REPLACE INTO symbol_live_scores (
                    symbol, direction, score, status, closed_trades, wins, losses,
                    win_rate, net_pnl, commission, funding_fee, profit_factor,
                    consecutive_wins, consecutive_losses, avg_hold_seconds,
                    last_hold_seconds,
                    penalty_until, last_trade_time, notes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    direction,
                    score["score"],
                    score["status"],
                    score["closed_trades"],
                    score["wins"],
                    score["losses"],
                    score["win_rate"],
                    score["net_pnl"],
                    score["commission"],
                    score["funding_fee"],
                    score["profit_factor"],
                    score["consecutive_wins"],
                    score["consecutive_losses"],
                    score["avg_hold_seconds"],
                    score["last_hold_seconds"],
                    score["penalty_until"],
                    score["last_trade_time"],
                    json.dumps(score["notes"], ensure_ascii=False),
                    now_iso(),
                ),
            )
            results.append({"symbol": symbol, "direction": direction, **score})
        for (symbol, direction, family), records in grouped_strategy.items():
            score = score_records(records, _strategy_score_config(config, family))
            conn.execute(
                """
                INSERT OR REPLACE INTO symbol_strategy_live_scores (
                    symbol, direction, strategy_family, score, status, closed_trades, wins, losses,
                    win_rate, net_pnl, commission, funding_fee, profit_factor,
                    consecutive_wins, consecutive_losses, avg_hold_seconds,
                    last_hold_seconds,
                    penalty_until, last_trade_time, notes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    direction,
                    family,
                    score["score"],
                    score["status"],
                    score["closed_trades"],
                    score["wins"],
                    score["losses"],
                    score["win_rate"],
                    score["net_pnl"],
                    score["commission"],
                    score["funding_fee"],
                    score["profit_factor"],
                    score["consecutive_wins"],
                    score["consecutive_losses"],
                    score["avg_hold_seconds"],
                    score["last_hold_seconds"],
                    score["penalty_until"],
                    score["last_trade_time"],
                    json.dumps(score["notes"], ensure_ascii=False),
                    now_iso(),
                ),
            )
        conn.commit()
    return sorted(results, key=lambda item: (item["score"], item["net_pnl"]), reverse=True)


def _parse_iso_ms(value: Any) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except ValueError:
        return None


def _strategy_score_config(config: dict[str, Any], family: str) -> dict[str, Any]:
    if family not in {ORDERBOOK_SCALP_FAMILY, EXTREME_V2_FAMILY, EXTREME_V3_FAMILY, EXTREME_V4_FAMILY}:
        return config
    scoped = dict(config)
    prefix = "scalp" if family == ORDERBOOK_SCALP_FAMILY else "v4" if family == EXTREME_V4_FAMILY else "v3" if family == EXTREME_V3_FAMILY else "extreme"
    defaults = {
        "scalp": (35, 5.0, 7.5, 12.0, 4.5, 1.0, 3.0, 4.0),
        "extreme": (300, 5.0, 7.0, 10.0, 3.0, 2.0, 6.0, 3.0),
        "v3": (300, 4.0, 6.0, 9.0, 3.0, 2.0, 6.0, 3.0),
        "v4": (300, 4.0, 6.0, 9.0, 3.0, 2.0, 6.0, 3.0),
    }[prefix]
    scoped["live_credit_quick_stop_seconds"] = int(config.get(f"{prefix}_credit_quick_stop_seconds", defaults[0]))
    scoped["live_credit_win_reward"] = float(config.get(f"{prefix}_credit_win_reward", defaults[1]))
    scoped["live_credit_loss_penalty"] = float(config.get(f"{prefix}_credit_loss_penalty", defaults[2]))
    scoped["live_credit_consecutive_loss_penalty"] = float(
        config.get(f"{prefix}_credit_consecutive_loss_penalty", defaults[3])
    )
    scoped["live_credit_fee_drag_penalty"] = float(config.get(f"{prefix}_credit_fee_drag_penalty", defaults[4]))
    scoped["live_credit_penalty_cooldown_hours"] = float(
        config.get(f"{prefix}_credit_penalty_cooldown_hours", defaults[5])
    )
    scoped["live_credit_recovery_interval_hours"] = float(
        config.get(f"{prefix}_credit_recovery_interval_hours", defaults[6])
    )
    scoped["live_credit_recovery_points"] = float(config.get(f"{prefix}_credit_recovery_points", defaults[7]))
    return scoped


def _strategy_family_from_payload(payload: dict[str, Any]) -> str:
    return _strategy_metadata_from_payload(payload)["strategy_family"]


def _strategy_metadata_from_payload(payload: dict[str, Any]) -> dict[str, str]:
    decision = payload.get("decision") if isinstance(payload, dict) else {}
    if not isinstance(decision, dict):
        return {"strategy_family": LEGACY_STRATEGY_FAMILY, "strategy_version": "legacy", "strategy_role": "legacy"}
    candidate = decision.get("candidate") or {}
    explicit = str(decision.get("strategy_family") or candidate.get("strategy_family") or "")
    version = str(decision.get("strategy_version") or candidate.get("strategy_version") or "legacy")
    role = str(decision.get("strategy_role") or candidate.get("strategy_role") or (ACTIVE_ROLE if version != "legacy" else "legacy"))
    if explicit in {ORDERBOOK_SCALP_FAMILY, EXTREME_V2_FAMILY, EXTREME_V3_FAMILY, EXTREME_V4_FAMILY, GRID_STRATEGY_FAMILY}:
        return {"strategy_family": explicit, "strategy_version": version, "strategy_role": role}
    if str(decision.get("strategy_generation") or candidate.get("strategy_generation") or "").lower() == "v3":
        return {"strategy_family": EXTREME_V3_FAMILY, "strategy_version": version, "strategy_role": role}
    entry_type = str(decision.get("entry_type") or candidate.get("entry_type") or "")
    mode = str(decision.get("mode") or ((decision.get("scan") or {}).get("mode") or {}).get("mode") or "")
    if mode == "yolo_scalp" and entry_type in ORDERBOOK_SCALP_ENTRY_TYPES:
        return {"strategy_family": ORDERBOOK_SCALP_FAMILY, "strategy_version": version, "strategy_role": role}
    if mode == "extreme_sprint":
        return {"strategy_family": EXTREME_V2_FAMILY, "strategy_version": version, "strategy_role": role}
    if mode == "grid":
        return {"strategy_family": GRID_STRATEGY_FAMILY, "strategy_version": version, "strategy_role": role}
    return {"strategy_family": LEGACY_STRATEGY_FAMILY, "strategy_version": "legacy", "strategy_role": "legacy"}


def _strategy_family_for_record(conn: sqlite3.Connection, record: dict[str, Any], config: dict[str, Any]) -> str:
    return _strategy_metadata_for_record(conn, record, config)["strategy_family"]


def _strategy_metadata_for_record(
    conn: sqlite3.Connection,
    record: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, str]:
    if not config.get("strategy_family_credit_enabled", True):
        return {"strategy_family": LEGACY_STRATEGY_FAMILY, "strategy_version": "legacy", "strategy_role": "legacy"}
    existing_family = str(record.get("strategy_family") or "")
    existing_version = str(record.get("strategy_version") or "")
    existing_role = str(record.get("strategy_role") or "")
    if (
        existing_family
        and existing_version
        and existing_role
        and existing_version != "legacy"
        and existing_role != "legacy"
    ):
        return {
            "strategy_family": existing_family,
            "strategy_version": existing_version,
            "strategy_role": existing_role,
        }
    symbol = str(record.get("symbol") or "").upper()
    direction = str(record.get("direction") or "").upper()
    open_time = int(record.get("open_time") or 0)
    if not symbol or direction not in {"LONG", "SHORT"} or open_time <= 0:
        return {"strategy_family": LEGACY_STRATEGY_FAMILY, "strategy_version": "legacy", "strategy_role": "legacy"}
    window_ms = int(float(config.get("strategy_credit_match_window_minutes", 30)) * 60_000)
    rows = conn.execute(
        """
        SELECT ts, payload FROM strategy_runs
        WHERE symbol = ? AND action = ?
        ORDER BY ts DESC
        LIMIT 80
        """,
        (symbol, f"OPEN_{direction}"),
    ).fetchall()
    best = {"strategy_family": LEGACY_STRATEGY_FAMILY, "strategy_version": "legacy", "strategy_role": "legacy"}
    best_distance = window_ms + 1
    for row in rows:
        ts_ms = _parse_iso_ms(row["ts"])
        if ts_ms is None:
            continue
        distance = abs(ts_ms - open_time)
        if distance > window_ms or distance >= best_distance:
            continue
        try:
            payload = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        best = _strategy_metadata_from_payload(payload)
        best_distance = distance
    return best


def enrich_live_score(item: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    recovery = recovered_score(float(item.get("score", DEFAULT_SCORE)), item.get("last_trade_time"), config)
    item["raw_score"] = item.get("score")
    item["score"] = recovery["score"]
    item["recovery_points"] = recovery["recovery_points"]
    item["next_recovery_at"] = recovery["next_recovery_at"]
    item["status"] = _status_for_score(float(item["score"]))
    item["status_label"] = status_label(item.get("status", ""))
    item["risk_multiplier"] = round(live_credit_multiplier(item, config), 4)
    item["cooldown_cap"] = round(cooldown_multiplier_cap(item, config), 4)
    item["cooldown"] = live_credit_cooldown_summary(item, config)
    return item


def list_live_scores(limit: int = 100, config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    config = config or {}
    init_live_learning_schema()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM symbol_live_scores ORDER BY score DESC, net_pnl DESC LIMIT ?",
            (limit,),
        ).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        try:
            item["notes"] = json.loads(item.get("notes") or "[]")
        except json.JSONDecodeError:
            item["notes"] = []
        item["last_trade_time_iso"] = _iso_from_ms(item.get("last_trade_time"))
        item = enrich_live_score(item, config)
        results.append(item)
    return results


def list_strategy_live_scores(
    limit: int = 100,
    config: dict[str, Any] | None = None,
    strategy_family: str | None = None,
) -> list[dict[str, Any]]:
    config = config or {}
    init_live_learning_schema()
    params: tuple[Any, ...]
    sql = "SELECT * FROM symbol_strategy_live_scores"
    if strategy_family:
        sql += " WHERE strategy_family = ?"
        params = (strategy_family, limit)
    else:
        params = (limit,)
    sql += " ORDER BY score DESC, net_pnl DESC LIMIT ?"
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        family = str(item.get("strategy_family") or LEGACY_STRATEGY_FAMILY)
        try:
            item["notes"] = json.loads(item.get("notes") or "[]")
        except json.JSONDecodeError:
            item["notes"] = []
        item["last_trade_time_iso"] = _iso_from_ms(item.get("last_trade_time"))
        item = enrich_live_score(item, _strategy_score_config(config, family))
        item["strategy_family"] = family
        results.append(item)
    return results


def live_score_for(symbol: str, direction: str, config: dict[str, Any]) -> dict[str, Any]:
    if not config.get("live_credit_enabled", True):
        return {"enabled": False, "score": DEFAULT_SCORE, "status": "normal", "status_label": "未启用"}
    init_live_learning_schema()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM symbol_live_scores WHERE symbol = ? AND direction = ?",
            (symbol.upper(), direction.upper()),
        ).fetchone()
    if not row:
        item = {
            "enabled": True,
            "score": float(config.get("live_credit_default_score", DEFAULT_SCORE)),
            "status": "new",
            "status_label": "新币观察",
            "closed_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "net_pnl": 0.0,
            "profit_factor": 0.0,
            "consecutive_wins": 0,
            "consecutive_losses": 0,
            "last_hold_seconds": 0.0,
            "penalty_until": None,
            "notes": ["暂无实盘记录"],
        }
        item["risk_multiplier"] = round(live_credit_multiplier(item, config), 4)
        item["cooldown_cap"] = round(cooldown_multiplier_cap(item, config), 4)
        item["cooldown"] = live_credit_cooldown_summary(item, config)
        return item
    item = dict(row)
    try:
        item["notes"] = json.loads(item.get("notes") or "[]")
    except json.JSONDecodeError:
        item["notes"] = []
    item["enabled"] = True
    return enrich_live_score(item, config)


def strategy_live_score_for(symbol: str, direction: str, strategy_family: str, config: dict[str, Any]) -> dict[str, Any]:
    family_enabled = config.get("strategy_family_credit_enabled", True)
    if strategy_family == ORDERBOOK_SCALP_FAMILY:
        family_enabled = family_enabled and config.get("yolo_scalp_strategy_credit_enabled", True)
    if not config.get("live_credit_enabled", True) or not family_enabled:
        return {"enabled": False, "score": DEFAULT_SCORE, "status": "normal", "status_label": "策略信用未启用"}
    init_live_learning_schema()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM symbol_strategy_live_scores
            WHERE symbol = ? AND direction = ? AND strategy_family = ?
            """,
            (symbol.upper(), direction.upper(), strategy_family),
        ).fetchone()
    if not row:
        item = _experiment_credit(symbol.upper(), direction.upper(), config, strategy_family)
        item["strategy_family"] = strategy_family
        item["risk_multiplier"] = round(live_credit_multiplier(item, config), 4)
        item["cooldown_cap"] = round(cooldown_multiplier_cap(item, config), 4)
        item["cooldown"] = live_credit_cooldown_summary(item, config)
        return item
    item = dict(row)
    try:
        item["notes"] = json.loads(item.get("notes") or "[]")
    except json.JSONDecodeError:
        item["notes"] = []
    item["last_trade_time_iso"] = _iso_from_ms(item.get("last_trade_time"))
    item = enrich_live_score(item, _strategy_score_config(config, strategy_family))
    item["strategy_family"] = strategy_family
    return item


def penalty_active(score: dict[str, Any]) -> bool:
    until = score.get("penalty_until")
    if not until:
        return False
    try:
        return datetime.fromisoformat(until) > datetime.now(timezone.utc)
    except ValueError:
        return False


def cooldown_multiplier_cap(score: dict[str, Any], config: dict[str, Any]) -> float:
    if not penalty_active(score):
        return float(config.get("live_credit_max_risk_multiplier", 2.0))
    losses = int(score.get("consecutive_losses") or 0)
    if losses >= 3:
        return float(config.get("live_credit_three_loss_cooldown_cap", 0.10))
    if losses >= 2:
        return float(config.get("live_credit_two_loss_cooldown_cap", 0.25))
    last_hold = float(score.get("last_hold_seconds") or score.get("avg_hold_seconds") or 0)
    if last_hold and last_hold <= float(config.get("live_credit_quick_stop_seconds", 60)):
        return float(config.get("live_credit_quick_loss_cooldown_cap", 0.40))
    return float(config.get("live_credit_loss_cooldown_cap", 0.60))


def live_credit_cooldown_summary(score: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    active = penalty_active(score)
    cap = cooldown_multiplier_cap(score, config)
    losses = int(score.get("consecutive_losses") or 0)
    last_hold = float(score.get("last_hold_seconds") or 0)
    kind = "none"
    if active:
        if losses >= 3:
            kind = "three_loss"
        elif losses >= 2:
            kind = "two_loss"
        elif last_hold and last_hold <= float(config.get("live_credit_quick_stop_seconds", 60)):
            kind = "quick_loss"
        else:
            kind = "loss"
    return {
        "active": active,
        "kind": kind,
        "cap": round(cap, 4),
        "penalty_until": score.get("penalty_until"),
    }


def live_credit_multiplier(score: dict[str, Any], config: dict[str, Any]) -> float:
    value = float(score.get("score", DEFAULT_SCORE))
    if value <= float(config.get("live_credit_fuse_score", 2.0)):
        return 0.0
    divisor = max(1.0, float(config.get("live_credit_multiplier_divisor", DEFAULT_SCORE)))
    max_multiplier = float(config.get("live_credit_max_risk_multiplier", 2.0))
    base = max(0.0, min(max_multiplier, value / divisor))
    return min(base, cooldown_multiplier_cap(score, config))


def live_credit_boost_qualified(score: dict[str, Any], config: dict[str, Any]) -> tuple[bool, list[str]]:
    if not config.get("live_credit_boost_requires_profitability", True):
        return True, []
    closed = int(score.get("closed_trades") or 0)
    net_pnl = float(score.get("net_pnl") or 0)
    profit_factor = float(score.get("profit_factor") or 0)
    commission = abs(float(score.get("commission") or 0))
    fee_to_net = commission / max(abs(net_pnl), 0.0001) if commission else 0.0
    failures: list[str] = []
    if closed < int(config.get("live_credit_boost_min_closed_trades", 3)):
        failures.append("sample_not_enough")
    if net_pnl < float(config.get("live_credit_boost_min_net_pnl_usdt", 0.2)):
        failures.append("net_profit_not_enough")
    if profit_factor < float(config.get("live_credit_boost_min_profit_factor", 1.2)):
        failures.append("profit_factor_not_enough")
    if fee_to_net > float(config.get("live_credit_boost_max_fee_to_net_ratio", 0.35)):
        failures.append("fee_drag_too_high")
    return not failures, failures


def apply_live_credit_to_candidate(candidate: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    if not config.get("live_credit_enabled", True):
        return candidate
    symbol = str(candidate.get("symbol") or "").upper()
    direction = str(candidate.get("direction") or "").upper()
    if not symbol or direction not in {"LONG", "SHORT"}:
        return candidate
    legacy_credit: dict[str, Any] | None = None
    strategy_family: str | None = None
    credit = live_score_for(symbol, direction, config)
    strategy_family = _strategy_family_for_candidate(candidate, config)
    if strategy_family:
        legacy_credit = credit
        credit = strategy_live_score_for(symbol, direction, strategy_family, config)
    score = float(credit.get("score", DEFAULT_SCORE))
    weight_key = (
        "scalp_credit_score_weight"
        if strategy_family == ORDERBOOK_SCALP_FAMILY
        else "v3_credit_score_weight"
        if strategy_family == EXTREME_V3_FAMILY
        else "v4_credit_score_weight"
        if strategy_family == EXTREME_V4_FAMILY
        else "extreme_credit_score_weight"
        if strategy_family == EXTREME_V2_FAMILY
        else "live_credit_score_weight"
    )
    weight = float(config.get(weight_key, config.get("live_credit_score_weight", 0.35)))
    score_delta = (score - float(config.get("live_credit_default_score", DEFAULT_SCORE))) * weight
    candidate = dict(candidate)
    input_risk_pct = float(candidate.get("risk_pct") or 0)
    candidate["live_credit"] = credit
    candidate["score"] = round(float(candidate.get("score") or 0) + score_delta, 4)

    reasons = [f"实盘信用 {score:.1f} 分（{credit.get('status_label', '-')}）"]
    multiplier = live_credit_multiplier(credit, config)
    cooldown = live_credit_cooldown_summary(credit, config)
    if strategy_family in {EXTREME_V3_FAMILY, EXTREME_V4_FAMILY}:
        prefix = "v4" if strategy_family == EXTREME_V4_FAMILY else "opportunity_v3"
        v3_floor = float(
            config.get(f"{prefix}_credit_cooldown_min_multiplier", 0.35)
            if cooldown.get("active") or int(credit.get("consecutive_losses") or 0) > 0
            else config.get(f"{prefix}_credit_min_multiplier", 0.80)
        )
        v3_ceiling = float(config.get(f"{prefix}_credit_max_multiplier", 1.20))
        multiplier = max(v3_floor, min(multiplier, v3_ceiling))
        reasons.append(f"{'V4' if strategy_family == EXTREME_V4_FAMILY else 'V3'} 独立信用倍率限制 {v3_floor:.2f}-{v3_ceiling:.2f}x")
    boost_ok, boost_failures = live_credit_boost_qualified(credit, config)
    if multiplier > 1.0 and not boost_ok:
        cap = float(config.get("live_credit_unqualified_boost_cap", 1.0))
        multiplier = min(multiplier, cap)
        reasons.append(f"未达盈利加仓条件：{','.join(boost_failures)}，倍率封顶 {cap:.2f}x")

    if int(credit.get("consecutive_wins") or 0) >= int(config.get("live_credit_tail_win_count", 3)):
        if boost_ok:
            streak_mult = float(config.get("live_credit_streak_profit_multiplier", 1.15))
            multiplier = min(float(config.get("live_credit_max_risk_multiplier", 2.0)), multiplier * streak_mult)
            reasons.append(f"连续盈利且净收益/PF达标，加仓 {streak_mult:.2f}x")
        else:
            tail_mult = float(config.get("live_credit_tail_risk_multiplier", 0.75))
            multiplier *= tail_mult
            candidate["score"] = round(float(candidate["score"]) - float(config.get("live_credit_tail_score_penalty", 3.0)), 4)
            reasons.append(f"连续盈利但质量未达标，防追尾 {tail_mult:.2f}x")

    if config.get("live_credit_fee_pressure_enabled", True):
        commission = abs(float(credit.get("commission") or 0))
        net_pnl = float(credit.get("net_pnl") or 0)
        fee_ratio = commission / max(abs(net_pnl), 0.0001) if commission > 0 else 0.0
        if net_pnl <= 0 and fee_ratio >= float(config.get("live_credit_fee_pressure_ratio", 0.20)):
            fee_mult = float(config.get("live_credit_fee_pressure_risk_multiplier", 0.75))
            multiplier *= fee_mult
            candidate["score"] = round(float(candidate["score"]) - min(8.0, fee_ratio * 4.0), 4)
            reasons.append(f"手续费压力 {fee_ratio:.2f}x，降仓 {fee_mult:.2f}x")

    legacy_soft_multiplier = 1.0
    legacy_soft_reasons: list[str] = []
    legacy_cooldown: dict[str, Any] | None = None
    if strategy_family == ORDERBOOK_SCALP_FAMILY and legacy_credit is not None and not config.get("yolo_scalp_strategy_credit_enabled", True):
        legacy_score = float(legacy_credit.get("score", DEFAULT_SCORE))
        legacy_cooldown = live_credit_cooldown_summary(legacy_credit, config)
        if legacy_score < float(config.get("yolo_scalp_legacy_credit_soft_score_threshold", 30.0)):
            legacy_soft_multiplier = min(
                legacy_soft_multiplier,
                float(config.get("yolo_scalp_legacy_credit_soft_multiplier", 0.70)),
            )
            legacy_soft_reasons.append(f"旧信用低分 {legacy_score:.1f}")
        if legacy_cooldown["active"] or int(legacy_credit.get("consecutive_losses") or 0) > 0:
            legacy_soft_multiplier = min(
                legacy_soft_multiplier,
                float(config.get("yolo_scalp_legacy_credit_soft_penalty_multiplier", 0.70)),
            )
            legacy_soft_reasons.append("旧策略亏损/冷却")
        if legacy_soft_multiplier < 1.0:
            multiplier *= legacy_soft_multiplier
            reasons.append(
                f"剥头皮试验期：旧信用仅软折扣 {legacy_soft_multiplier:.2f}x（{','.join(legacy_soft_reasons)}）"
            )
        else:
            reasons.append("剥头皮试验期：旧信用不硬拦截")
        candidate["legacy_live_credit"] = legacy_credit
    elif legacy_credit is not None:
        label = "剥头皮" if strategy_family == ORDERBOOK_SCALP_FAMILY else "机会引擎 V4" if strategy_family == EXTREME_V4_FAMILY else "机会引擎 V3" if strategy_family == EXTREME_V3_FAMILY else "极限 V2" if strategy_family == EXTREME_V2_FAMILY else "当前策略"
        reasons.append(f"{label}独立信用：旧策略信用仅展示，不参与仓位")
        candidate["legacy_live_credit"] = legacy_credit

    bypass_allowed = False
    if cooldown["active"]:
        reasons.append(f"冷却倍率上限 {cooldown['cap']:.2f}x，冷却到 {credit.get('penalty_until')}")
        bypass_allowed = (
            config.get("live_credit_cooldown_bypass_enabled", True)
            and float(candidate.get("score") or 0) >= float(config.get("live_credit_cooldown_bypass_min_score", 95.0))
            and float(candidate.get("cost_ratio") or 0) >= float(config.get("live_credit_cooldown_bypass_min_cost_ratio", 18.0))
            and multiplier > 0
        )
        if bypass_allowed:
            reasons.append("强信号穿透冷却，但仓位仍受冷却倍率限制")

    if multiplier <= 0:
        candidate["passed"] = False
        candidate["reason"] = "live_credit_fuse"
        candidate["decision_reason"] = "实盘信用接近 0 分，熔断等待自然恢复"
    else:
        candidate["risk_pct"] = float(candidate.get("risk_pct") or 0) * multiplier
        if str(candidate.get("entry_type") or "") == "extreme_probe":
            is_yolo = str(candidate.get("mode") or "") == "yolo_scalp"
            closed = int(credit.get("closed_trades") or 0)
            losses = int(credit.get("losses") or 0)
            if closed <= 0:
                cap = float(
                    config.get("yolo_scalp_firecracker_max_risk_pct", 70.0)
                    if is_yolo
                    else config.get("extreme_probe_new_symbol_max_risk_pct", 2.2)
                )
                if candidate["risk_pct"] > cap:
                    candidate["risk_pct"] = cap
                    reasons.append(("yolo firecracker cap" if is_yolo else "new probe cap") + f" {cap:.2f}%")
            if losses > 0 or cooldown["active"]:
                loss_mult = float(
                    config.get("yolo_scalp_loss_probe_risk_multiplier", 0.75)
                    if is_yolo
                    else config.get("extreme_probe_loss_risk_multiplier", 0.55)
                )
                candidate["risk_pct"] *= loss_mult
                cap = float(
                    config.get("yolo_scalp_loss_probe_max_risk_pct", 12.0)
                    if is_yolo
                    else config.get("extreme_probe_after_loss_max_risk_pct", 1.2)
                )
                if candidate["risk_pct"] > cap:
                    candidate["risk_pct"] = cap
                candidate["score"] = round(float(candidate["score"]) - min(10.0, 3.0 + losses * 2.0), 4)
                reasons.append(f"{'yolo probe after loss' if is_yolo else 'probe after loss'} {loss_mult:.2f}x, cap {cap:.2f}%")
            candidate["risk_pct"] = round(float(candidate["risk_pct"]), 8)
        reasons.append(f"仓位倍率 {multiplier:.2f}x")
        if not candidate.get("decision_reason"):
            candidate["decision_reason"] = "；".join(reasons)
        else:
            candidate["decision_reason"] = f"{candidate['decision_reason']}；{'；'.join(reasons)}"
    candidate["live_credit_adjustment"] = {
        "input_risk_pct": round(input_risk_pct, 8),
        "score_delta": round(score_delta, 4),
        "risk_multiplier": round(multiplier, 4),
        "boost_qualified": boost_ok,
        "boost_failures": boost_failures,
        "cooldown": cooldown,
        "cooldown_bypass": bypass_allowed,
        "reasons": reasons,
        "strategy_family": strategy_family,
        "legacy_soft_multiplier": round(legacy_soft_multiplier, 4),
        "legacy_soft_reasons": legacy_soft_reasons,
        "legacy_cooldown": legacy_cooldown,
    }
    return candidate

def _signed_open(position_side: str, side: str, quantity: float) -> float:
    if position_side == "LONG":
        return quantity if side == "BUY" else -quantity
    if position_side == "SHORT":
        return quantity if side == "SELL" else -quantity
    return quantity if side == "BUY" else -quantity


def build_trade_records_from_user_trades(
    trades_by_symbol: dict[str, list[dict[str, Any]]],
    income_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    funding_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in income_rows or []:
        if str(row.get("incomeType")) == "FUNDING_FEE" and row.get("symbol"):
            funding_by_symbol.setdefault(str(row["symbol"]).upper(), []).append(row)
    records: list[dict[str, Any]] = []
    for symbol, raw_trades in trades_by_symbol.items():
        active: dict[str, dict[str, Any]] = {}
        for trade in sorted(raw_trades, key=lambda item: int(item.get("time", 0))):
            direction = str(trade.get("positionSide") or "").upper()
            if direction not in {"LONG", "SHORT"}:
                direction = "LONG" if str(trade.get("side")).upper() == "BUY" else "SHORT"
            side = str(trade.get("side") or "").upper()
            quantity = float(trade.get("qty") or trade.get("quantity") or 0)
            price = float(trade.get("price") or 0)
            trade_time = int(trade.get("time") or 0)
            commission = abs(float(trade.get("commission") or 0))
            realized = float(trade.get("realizedPnl") or 0)
            signed = _signed_open(direction, side, quantity)
            item = active.get(direction)
            if item is None and signed > 0:
                item = {
                    "symbol": symbol.upper(),
                    "direction": direction,
                    "open_time": trade_time,
                    "open_notional": 0.0,
                    "close_notional": 0.0,
                    "quantity": 0.0,
                    "close_quantity": 0.0,
                    "realized_pnl": 0.0,
                    "commission": 0.0,
                    "entry_commission": 0.0,
                    "close_commission": 0.0,
                    "trade_count": 0,
                    "entry_order_ids": [],
                    "exit_order_ids": [],
                    "payload": {"fills": []},
                }
                active[direction] = item
            if item is None:
                continue
            item["commission"] += commission
            item["realized_pnl"] += realized
            item["trade_count"] += 1
            item["payload"]["fills"].append(
                {
                    "orderId": trade.get("orderId"),
                    "time": trade_time,
                    "side": side,
                    "qty": quantity,
                    "price": price,
                    "realizedPnl": realized,
                    "commission": commission,
                }
            )
            if signed > 0:
                order_id = trade.get("orderId")
                if order_id is not None and str(order_id) not in item["entry_order_ids"]:
                    item["entry_order_ids"].append(str(order_id))
                item["entry_commission"] += commission
                item["quantity"] += quantity
                item["open_notional"] += quantity * price
            else:
                order_id = trade.get("orderId")
                if order_id is not None and str(order_id) not in item["exit_order_ids"]:
                    item["exit_order_ids"].append(str(order_id))
                item["close_commission"] += commission
                item["close_quantity"] += quantity
                item["close_notional"] += quantity * price
                item["close_time"] = trade_time
            if item["quantity"] > 0 and item.get("close_quantity", 0.0) >= item["quantity"] - 1e-12:
                funding_fee = 0.0
                for funding in funding_by_symbol.get(symbol.upper(), []):
                    funding_time = int(funding.get("time") or 0)
                    if int(item["open_time"]) <= funding_time <= int(item["close_time"]):
                        funding_fee += float(funding.get("income") or 0)
                open_price = item["open_notional"] / item["quantity"] if item["quantity"] else 0.0
                close_price = item["close_notional"] / item["close_quantity"] if item["close_quantity"] else 0.0
                net_pnl = float(item["realized_pnl"]) - abs(float(item["commission"])) + funding_fee
                records.append(
                    {
                        **item,
                        "open_price": open_price,
                        "close_price": close_price,
                        "funding_fee": funding_fee,
                        "net_pnl": net_pnl,
                        "hold_seconds": (int(item["close_time"]) - int(item["open_time"])) / 1000,
                        "source": "binance",
                    }
                )
                active.pop(direction, None)
    return records


def sync_live_learning_from_binance(
    client: BinanceFuturesClient,
    config: dict[str, Any],
    lookback_hours: float | None = None,
) -> dict[str, Any]:
    if not config.get("live_credit_enabled", True):
        return {"enabled": False, "reason": "disabled"}
    lookback_hours = lookback_hours or float(config.get("live_credit_history_hours", 96))
    end_ms = int(time.time() * 1000)
    start_ms = int((time.time() - lookback_hours * 3600) * 1000)
    income = client.signed_request("GET", "/fapi/v1/income", {"startTime": start_ms, "endTime": end_ms, "limit": 1000})
    symbols = sorted({str(row.get("symbol", "")).upper() for row in income if row.get("symbol")})
    max_symbols = int(config.get("live_credit_sync_max_symbols", 20))
    symbols = symbols[:max_symbols]
    trades_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for symbol in symbols:
        rows = client.signed_request(
            "GET",
            "/fapi/v1/userTrades",
            {"symbol": symbol, "startTime": start_ms, "endTime": end_ms, "limit": 1000},
        )
        if rows:
            trades_by_symbol[symbol] = rows
    records = build_trade_records_from_user_trades(trades_by_symbol, income)
    for record in records:
        try:
            record.update(match_trade_record(record))
            if record.get("strategy_family") and record.get("strategy_version"):
                record["release_id"] = release_id(
                    str(record["strategy_family"]),
                    str(record["strategy_version"]),
                )
            finalize_trade_lineage(record)
        except sqlite3.OperationalError:
            record["lineage_quality"] = "unavailable"
    upserted = upsert_trade_records(records)
    scores = rebuild_symbol_scores(config, lookback_hours=lookback_hours)
    result = {
        "enabled": True,
        "lookback_hours": lookback_hours,
        "symbols": symbols,
        "records": upserted,
        "scores": scores,
    }
    record_event("info", "live_learning", "实盘信用分已同步", result)
    return result
