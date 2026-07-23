from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timezone
from typing import Any

from app.market_stream import read_snapshot, stream_kline_history
from app.telemetry import connect, now_iso


FEATURE_SCHEMA_VERSION = "s0-minute-v1"


def _add_column(conn: Any, table: str, definition: str) -> None:
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
    except Exception as exc:
        if "duplicate column" not in str(exc).lower():
            raise


def _table_exists(conn: Any, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
    )


def init_training_lineage_schema() -> None:
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS opportunity_lineage (
                opportunity_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                signal_type TEXT,
                setup_type TEXT,
                market_regime TEXT,
                strategy_family TEXT,
                strategy_version TEXT,
                strategy_role TEXT,
                admission_lane TEXT,
                decision_action TEXT,
                decision_status TEXT,
                signal_time_ms INTEGER,
                decision_price REAL,
                stop_price REAL,
                take_profit_price REAL,
                requested_quantity REAL,
                requested_notional REAL,
                leverage REAL,
                risk_pct REAL,
                entry_order_id TEXT,
                entry_client_order_id TEXT,
                stop_order_id TEXT,
                take_profit_order_id TEXT,
                entry_fill_time INTEGER,
                entry_fill_price REAL,
                entry_fill_quantity REAL,
                entry_commission REAL,
                close_fill_time INTEGER,
                close_fill_price REAL,
                realized_pnl REAL,
                close_commission REAL,
                funding_fee REAL,
                net_pnl REAL,
                entry_slippage_bps REAL,
                hold_seconds REAL,
                exit_reason TEXT,
                match_quality TEXT,
                feature_schema_version TEXT,
                minute_features TEXT,
                decision_payload TEXT,
                execution_payload TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_opportunity_lineage_symbol_time "
            "ON opportunity_lineage(symbol, direction, signal_time_ms DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_opportunity_lineage_entry_order "
            "ON opportunity_lineage(entry_order_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_opportunity_lineage_version "
            "ON opportunity_lineage(strategy_family, strategy_version, decision_status, signal_time_ms DESC)"
        )
        conn.commit()


def ensure_live_lineage_columns(conn: Any) -> None:
    if not _table_exists(conn, "live_trade_records"):
        return
    _add_column(conn, "live_trade_records", "opportunity_id TEXT")
    _add_column(conn, "live_trade_records", "entry_order_ids TEXT")
    _add_column(conn, "live_trade_records", "exit_order_ids TEXT")
    _add_column(conn, "live_trade_records", "entry_slippage_bps REAL")
    _add_column(conn, "live_trade_records", "exit_reason TEXT")
    _add_column(conn, "live_trade_records", "lineage_quality TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_live_trade_opportunity "
        "ON live_trade_records(opportunity_id, close_time DESC)"
    )


def ensure_opportunity_id(candidate: dict[str, Any]) -> str:
    current = str(candidate.get("opportunity_id") or "").strip()
    if current:
        return current
    symbol = str(candidate.get("symbol") or "UNKNOWN").upper()
    direction = str(candidate.get("direction") or "WAIT").upper()
    created_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    value = f"{symbol}:{direction}:{created_ms}:{uuid.uuid4().hex[:10]}"
    candidate["opportunity_id"] = value
    return value


def _float(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _bar_features(rows: list[list[Any]]) -> dict[str, Any]:
    closes = [_float(row[4]) for row in rows if len(row) > 7]
    volumes = [_float(row[7]) for row in rows if len(row) > 7]
    closes = [value for value in closes if value is not None and value > 0]
    volumes = [value for value in volumes if value is not None and value >= 0]
    returns_bps = [
        (closes[index] / closes[index - 1] - 1) * 10_000
        for index in range(1, len(closes))
        if closes[index - 1] > 0
    ]
    mean_volume = sum(volumes[:-1]) / len(volumes[:-1]) if len(volumes) > 1 else 0.0
    return {
        "bar_count": len(rows),
        "returns_bps": [round(value, 6) for value in returns_bps[-11:]],
        "one_minute_return_bps": round(returns_bps[-1], 6) if returns_bps else None,
        "five_minute_return_bps": round(sum(returns_bps[-5:]), 6) if returns_bps else None,
        "realized_move_bps": round(sum(abs(value) for value in returns_bps[-5:]), 6) if returns_bps else None,
        "quote_volume_ratio": round(volumes[-1] / mean_volume, 6) if volumes and mean_volume > 0 else None,
        "last_close": closes[-1] if closes else None,
        "rows": rows[-12:],
    }


def capture_minute_features(candidate: dict[str, Any]) -> dict[str, Any]:
    symbol = str(candidate.get("symbol") or "").upper()
    stream = read_snapshot()
    rows = stream_kline_history(symbol, "1m", limit=12, max_age_seconds=90)
    depth = dict((stream.get("depths") or {}).get(symbol) or candidate.get("depth") or {})
    signal = candidate.get("signal") or {}
    structure = candidate.get("market_structure") or {}
    v4 = candidate.get("opportunity_v4") or {}
    return {
        "schema": FEATURE_SCHEMA_VERSION,
        "captured_at": now_iso(),
        "stream_updated_at": stream.get("updated_at"),
        "symbol": symbol,
        "direction": str(candidate.get("direction") or signal.get("signal") or "").upper(),
        "bars_1m": _bar_features(rows),
        "spread_pct": _float(depth.get("spread_pct")),
        "depth_notional": _float(depth.get("depth_notional")),
        "microprice_edge_bps": _float(depth.get("microprice_edge_bps")),
        "trade_flow_imbalance": _float(depth.get("trade_flow_imbalance")),
        "trade_flow_notional": _float(depth.get("trade_flow_notional")),
        "signal": {
            "atr_pct": _float(signal.get("atr_pct")),
            "volume_acceleration": _float(signal.get("volume_acceleration")),
            "directed_trade_flow": _float(signal.get("directed_trade_flow")),
            "impulse_atr": _float(signal.get("impulse_atr")),
            "breakout_extension_atr": _float(signal.get("breakout_extension_atr")),
            "entry_phase": signal.get("entry_phase"),
        },
        "model": {
            "score": _float(v4.get("score") or candidate.get("score")),
            "rank_percentile": _float(v4.get("rank_percentile")),
            "expected_net_pct": _float(v4.get("expected_net_pct")),
            "lower_expected_net_pct": _float(v4.get("lower_expected_net_pct")),
            "admission_lane": v4.get("admission_lane"),
        },
        "market_structure": {
            "market_regime": structure.get("market_regime"),
            "setup_type": structure.get("setup_type"),
            "entry_phase": structure.get("entry_phase"),
            "medium_trend_aligned": structure.get("medium_trend_aligned"),
            "medium_path_efficiency": _float(structure.get("medium_path_efficiency")),
        },
    }


def _compact_decision(decision: dict[str, Any]) -> dict[str, Any]:
    candidate = decision.get("candidate") or {}
    signal = decision.get("signal") or candidate.get("signal") or {}
    v4 = candidate.get("opportunity_v4") or {}
    return {
        "action": decision.get("action"),
        "reason": decision.get("reason") or (decision.get("risk") or {}).get("reason"),
        "quantity": decision.get("quantity"),
        "leverage": decision.get("leverage"),
        "risk_pct": decision.get("risk_pct"),
        "signal": {
            key: signal.get(key)
            for key in ("signal", "entry_type", "last_price", "stop", "take_profit", "atr", "atr_pct")
            if key in signal
        },
        "candidate": {
            "score": candidate.get("score"),
            "passed": candidate.get("passed"),
            "strategy_family": candidate.get("strategy_family"),
            "strategy_version": candidate.get("strategy_version"),
            "strategy_role": candidate.get("strategy_role"),
            "entry_type": candidate.get("entry_type"),
            "cost_ratio": candidate.get("cost_ratio"),
            "opportunity_v4": {
                key: v4.get(key)
                for key in (
                    "score",
                    "rank_percentile",
                    "rank_bucket",
                    "admission_lane",
                    "expected_net_pct",
                    "lower_expected_net_pct",
                    "market_regime",
                    "setup_type",
                )
                if key in v4
            },
        },
    }


def record_decision_opportunity(decision: dict[str, Any]) -> str | None:
    candidate = decision.get("candidate") or {}
    if not candidate or not candidate.get("symbol"):
        return None
    opportunity_id = ensure_opportunity_id(candidate)
    decision["opportunity_id"] = opportunity_id
    signal = decision.get("signal") or candidate.get("signal") or {}
    v4 = candidate.get("opportunity_v4") or {}
    structure = candidate.get("market_structure") or {}
    features = capture_minute_features(candidate)
    signal_time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    price = _float(signal.get("last_price"))
    quantity = _float(decision.get("quantity"))
    with connect() as conn:
        init_training_lineage_schema()
        conn.execute(
            """
            INSERT INTO opportunity_lineage (
                opportunity_id, created_at, updated_at, symbol, direction, signal_type,
                setup_type, market_regime, strategy_family, strategy_version, strategy_role,
                admission_lane, decision_action, decision_status, signal_time_ms,
                decision_price, stop_price, take_profit_price, requested_quantity,
                requested_notional, leverage, risk_pct, feature_schema_version,
                minute_features, decision_payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(opportunity_id) DO UPDATE SET
                updated_at = excluded.updated_at,
                strategy_family = excluded.strategy_family,
                strategy_version = excluded.strategy_version,
                strategy_role = excluded.strategy_role,
                admission_lane = excluded.admission_lane,
                decision_action = excluded.decision_action,
                decision_status = excluded.decision_status,
                minute_features = excluded.minute_features,
                decision_payload = excluded.decision_payload
            """,
            (
                opportunity_id,
                now_iso(),
                now_iso(),
                str(candidate.get("symbol") or "").upper(),
                str(candidate.get("direction") or signal.get("signal") or "").upper(),
                str(candidate.get("entry_type") or signal.get("entry_type") or ""),
                str(structure.get("setup_type") or v4.get("setup_type") or ""),
                str(structure.get("market_regime") or v4.get("market_regime") or ""),
                str(candidate.get("strategy_family") or ""),
                str(candidate.get("strategy_version") or ""),
                str(candidate.get("strategy_role") or ""),
                str(v4.get("admission_lane") or ""),
                str(decision.get("action") or ""),
                "DECIDED",
                signal_time_ms,
                price,
                _float(signal.get("stop")),
                _float(signal.get("take_profit")),
                quantity,
                quantity * price if quantity is not None and price is not None else None,
                _float(decision.get("leverage")),
                _float((decision.get("effective_risk") or {}).get("final_risk_pct") or decision.get("risk_pct")),
                FEATURE_SCHEMA_VERSION,
                json.dumps(features, ensure_ascii=False, separators=(",", ":")),
                json.dumps(_compact_decision(decision), ensure_ascii=False, separators=(",", ":")),
            ),
        )
        conn.commit()
    return opportunity_id


def record_shadow_opportunity(conn: Any, candidate: dict[str, Any], opportunity_id: str) -> None:
    signal = candidate.get("signal") or {}
    v4 = candidate.get("opportunity_v4") or {}
    structure = candidate.get("market_structure") or {}
    features = capture_minute_features(candidate)
    signal_time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    conn.execute(
        """
        INSERT INTO opportunity_lineage (
            opportunity_id, created_at, updated_at, symbol, direction, signal_type,
            setup_type, market_regime, strategy_family, strategy_version, strategy_role,
            admission_lane, decision_action, decision_status, signal_time_ms,
            decision_price, stop_price, take_profit_price, risk_pct,
            feature_schema_version, minute_features, decision_payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SHADOW', ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(opportunity_id) DO UPDATE SET
            updated_at = excluded.updated_at,
            minute_features = COALESCE(opportunity_lineage.minute_features, excluded.minute_features)
        """,
        (
            opportunity_id,
            now_iso(),
            now_iso(),
            str(candidate.get("symbol") or "").upper(),
            str(candidate.get("direction") or signal.get("signal") or "").upper(),
            str(candidate.get("entry_type") or signal.get("entry_type") or ""),
            str(structure.get("setup_type") or v4.get("setup_type") or ""),
            str(structure.get("market_regime") or v4.get("market_regime") or ""),
            str(candidate.get("strategy_family") or ""),
            str(candidate.get("strategy_version") or ""),
            str(candidate.get("strategy_role") or ""),
            str(v4.get("admission_lane") or ""),
            "SHADOW_CANDIDATE",
            signal_time_ms,
            _float(signal.get("last_price")),
            _float(signal.get("stop")),
            _float(signal.get("take_profit")),
            _float(candidate.get("risk_pct")),
            FEATURE_SCHEMA_VERSION,
            json.dumps(features, ensure_ascii=False, separators=(",", ":")),
            json.dumps(
                {
                    "score": candidate.get("score"),
                    "passed": candidate.get("passed"),
                    "decision_reason": candidate.get("decision_reason"),
                    "evidence_type": candidate.get("evidence_type"),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        ),
    )


def _order_id(order: dict[str, Any] | None) -> str | None:
    order = order or {}
    value = order.get("orderId") or order.get("algoId")
    return str(value) if value is not None else None


def record_execution_result(decision: dict[str, Any], result: dict[str, Any]) -> None:
    opportunity_id = str(decision.get("opportunity_id") or (decision.get("candidate") or {}).get("opportunity_id") or "")
    if not opportunity_id:
        return
    entry = result.get("entry_order") or {}
    status = "EXECUTED" if result.get("mode") in {"live", "rotation_live"} else str(result.get("mode") or "BLOCKED").upper()
    with connect() as conn:
        init_training_lineage_schema()
        conn.execute(
            """
            UPDATE opportunity_lineage
            SET updated_at = ?, decision_status = ?, entry_order_id = ?,
                entry_client_order_id = ?, stop_order_id = ?, take_profit_order_id = ?,
                execution_payload = ?
            WHERE opportunity_id = ?
            """,
            (
                now_iso(),
                status,
                _order_id(entry),
                str(entry.get("clientOrderId") or "") or None,
                _order_id(result.get("stop_order")),
                _order_id(result.get("take_profit_order")),
                json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str),
                opportunity_id,
            ),
        )
        conn.commit()


def match_trade_record(record: dict[str, Any]) -> dict[str, Any]:
    entry_ids = [str(value) for value in record.get("entry_order_ids") or [] if value is not None]
    with connect() as conn:
        init_training_lineage_schema()
        row = None
        if entry_ids:
            placeholders = ",".join("?" for _ in entry_ids)
            row = conn.execute(
                f"SELECT * FROM opportunity_lineage WHERE entry_order_id IN ({placeholders}) "
                "ORDER BY signal_time_ms DESC LIMIT 1",
                entry_ids,
            ).fetchone()
        quality = "exact_order_id" if row else None
        if row is None:
            row = conn.execute(
                """
                SELECT * FROM opportunity_lineage
                WHERE symbol = ? AND direction = ? AND signal_time_ms BETWEEN ? AND ?
                  AND decision_status IN ('EXECUTED', 'CLOSED')
                ORDER BY ABS(signal_time_ms - ?) ASC LIMIT 1
                """,
                (
                    str(record.get("symbol") or "").upper(),
                    str(record.get("direction") or "").upper(),
                    int(record.get("open_time") or 0) - 120_000,
                    int(record.get("open_time") or 0) + 120_000,
                    int(record.get("open_time") or 0),
                ),
            ).fetchone()
            quality = "approximate_time" if row else "unmatched"
    return {
        "opportunity_id": row["opportunity_id"] if row else None,
        "lineage_quality": quality,
        "strategy_family": row["strategy_family"] if row else None,
        "strategy_version": row["strategy_version"] if row else None,
        "strategy_role": row["strategy_role"] if row else None,
    }


def _infer_exit_reason(row: dict[str, Any], close_price: float | None) -> str:
    if close_price is None:
        return "unknown"
    stop = _float(row.get("stop_price"))
    take = _float(row.get("take_profit_price"))
    direction = str(row.get("direction") or "").upper()
    if stop and abs(close_price - stop) / stop <= 0.004:
        return "stop_loss"
    if take and abs(close_price - take) / take <= 0.004:
        return "take_profit"
    if direction == "LONG" and stop and close_price <= stop:
        return "stop_loss"
    if direction == "SHORT" and stop and close_price >= stop:
        return "stop_loss"
    return "other_or_runtime"


def finalize_trade_lineage(record: dict[str, Any]) -> None:
    opportunity_id = str(record.get("opportunity_id") or "")
    if not opportunity_id:
        return
    with connect() as conn:
        init_training_lineage_schema()
        found = conn.execute(
            "SELECT * FROM opportunity_lineage WHERE opportunity_id = ?",
            (opportunity_id,),
        ).fetchone()
        if not found:
            return
        row = dict(found)
        decision_price = _float(row.get("decision_price"))
        entry_price = _float(record.get("open_price"))
        direction = str(record.get("direction") or "").upper()
        sign = -1.0 if direction == "SHORT" else 1.0
        slippage = (
            sign * (entry_price - decision_price) / decision_price * 10_000
            if entry_price is not None and decision_price not in {None, 0}
            else None
        )
        exit_reason = _infer_exit_reason(row, _float(record.get("close_price")))
        conn.execute(
            """
            UPDATE opportunity_lineage
            SET updated_at = ?, decision_status = 'CLOSED', entry_fill_time = ?,
                entry_fill_price = ?, entry_fill_quantity = ?, entry_commission = ?,
                close_fill_time = ?, close_fill_price = ?, realized_pnl = ?,
                close_commission = ?, funding_fee = ?, net_pnl = ?,
                entry_slippage_bps = ?, hold_seconds = ?, exit_reason = ?, match_quality = ?
            WHERE opportunity_id = ?
            """,
            (
                now_iso(),
                record.get("open_time"),
                entry_price,
                record.get("quantity"),
                record.get("entry_commission"),
                record.get("close_time"),
                record.get("close_price"),
                record.get("realized_pnl"),
                record.get("close_commission"),
                record.get("funding_fee"),
                record.get("net_pnl"),
                slippage,
                record.get("hold_seconds"),
                exit_reason,
                record.get("lineage_quality"),
                opportunity_id,
            ),
        )
        conn.commit()
    record["entry_slippage_bps"] = slippage
    record["exit_reason"] = exit_reason


def training_data_quality() -> dict[str, Any]:
    init_training_lineage_schema()
    with connect() as conn:
        ensure_live_lineage_columns(conn)
        lineage = dict(
            conn.execute(
                """
                SELECT COUNT(*) total,
                       SUM(CASE WHEN decision_status IN ('EXECUTED', 'CLOSED') THEN 1 ELSE 0 END) executed,
                       SUM(CASE WHEN decision_status = 'CLOSED' THEN 1 ELSE 0 END) closed,
                       SUM(CASE WHEN match_quality = 'exact_order_id' THEN 1 ELSE 0 END) exact_matches,
                       SUM(CASE WHEN minute_features IS NOT NULL AND minute_features != '' THEN 1 ELSE 0 END) feature_rows
                FROM opportunity_lineage
                """
            ).fetchone()
        )
        live = (
            dict(conn.execute(
                """
                SELECT COUNT(*) total,
                       SUM(CASE WHEN opportunity_id IS NOT NULL AND opportunity_id != '' THEN 1 ELSE 0 END) linked,
                       SUM(CASE WHEN lineage_quality = 'exact_order_id' THEN 1 ELSE 0 END) exact_matches,
                       SUM(CASE WHEN opportunity_id IS NULL OR opportunity_id = '' THEN 1 ELSE 0 END) unmatched,
                       SUM(CASE WHEN entry_slippage_bps IS NOT NULL THEN 1 ELSE 0 END) slippage_rows
                FROM live_trade_records
                """
            ).fetchone())
            if _table_exists(conn, "live_trade_records")
            else {"total": 0, "linked": 0, "exact_matches": 0, "unmatched": 0, "slippage_rows": 0}
        )
        shadow = (
            dict(conn.execute(
                """
                SELECT COUNT(*) total,
                       SUM(CASE WHEN opportunity_id IS NOT NULL AND opportunity_id != '' THEN 1 ELSE 0 END) linked,
                       SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END) closed
                FROM shadow_trades
                """
            ).fetchone())
            if _table_exists(conn, "shadow_trades")
            else {"total": 0, "linked": 0, "closed": 0}
        )
    exact = int(live.get("exact_matches") or 0)
    total_live = int(live.get("total") or 0)
    ready = exact >= 30 and int(lineage.get("feature_rows") or 0) >= 100
    return {
        "schema_version": FEATURE_SCHEMA_VERSION,
        "generated_at": now_iso(),
        "lineage": {key: int(value or 0) for key, value in lineage.items()},
        "live": {key: int(value or 0) for key, value in live.items()},
        "shadow": {key: int(value or 0) for key, value in shadow.items()},
        "high_weight_training_ready": ready,
        "exact_live_link_rate_pct": round(exact / total_live * 100, 2) if total_live else 0.0,
        "message": (
            "高权重实盘样本已达到首轮训练门槛。"
            if ready
            else "正在积累带机会编号、分钟快照和真实成本的高权重样本。"
        ),
    }


def training_dataset(limit: int = 5000) -> list[dict[str, Any]]:
    init_training_lineage_schema()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM opportunity_lineage
            WHERE minute_features IS NOT NULL
            ORDER BY signal_time_ms DESC LIMIT ?
            """,
            (max(1, min(int(limit), 50_000)),),
        ).fetchall()
        shadow_by_opportunity: dict[str, dict[str, Any]] = {}
        if rows and _table_exists(conn, "shadow_trades"):
            ids = [str(row["opportunity_id"]) for row in rows]
            for offset in range(0, len(ids), 500):
                batch = ids[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                shadows = conn.execute(
                    f"""
                    SELECT status, opened_at, closed_at, entry, last_price, gross_pnl,
                           estimated_cost, net_pnl, outcome, evidence_type, opportunity_id
                    FROM shadow_trades WHERE opportunity_id IN ({placeholders})
                    ORDER BY id DESC
                    """,
                    batch,
                ).fetchall()
                for shadow in shadows:
                    key = str(shadow["opportunity_id"])
                    shadow_by_opportunity.setdefault(key, dict(shadow))
    result = []
    for raw in rows:
        item = dict(raw)
        for key in ("minute_features", "decision_payload", "execution_payload"):
            try:
                item[key] = json.loads(item.get(key) or "{}")
            except (TypeError, ValueError):
                item[key] = {}
        item["shadow_result"] = shadow_by_opportunity.get(str(item["opportunity_id"]))
        result.append(item)
    return result
