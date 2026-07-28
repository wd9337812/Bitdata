from __future__ import annotations

from datetime import datetime, timezone

from app.live_learning import (
    build_trade_records_from_user_trades,
    init_live_learning_schema,
    upsert_trade_records,
)
from app.market_stream import write_snapshot
from app.shadow_trading import ensure_shadow_tables
from app.telemetry import connect
from app.training_lineage import (
    ensure_event_group_id,
    ensure_event_id,
    finalize_trade_lineage,
    match_trade_record,
    record_decision_opportunity,
    record_execution_result,
    training_data_quality,
)


def test_event_group_merges_setup_variants_but_event_id_does_not():
    first = {
        "symbol": "SOLUSDT",
        "direction": "LONG",
        "entry_type": "trend_breakout",
        "signal_time_ms": 1_800_000,
    }
    second = {
        "symbol": "SOLUSDT",
        "direction": "LONG",
        "entry_type": "trend_pullback",
        "signal_time_ms": 1_800_000,
    }

    assert ensure_event_id(first) != ensure_event_id(second)
    assert ensure_event_group_id(first) == ensure_event_group_id(second)


def _decision() -> dict:
    candidate = {
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "entry_type": "trend_breakout",
        "strategy_family": "extreme_v4_roll",
        "strategy_version": "v4.10",
        "strategy_role": "active",
        "score": 78,
        "passed": True,
        "market_structure": {
            "setup_type": "trend_breakout",
            "market_regime": "trend",
            "entry_phase": "TRIGGERED",
        },
        "opportunity_v4": {
            "score": 78,
            "rank_percentile": 0.04,
            "admission_lane": "full_bet",
            "expected_net_pct": 0.2,
            "lower_expected_net_pct": 0.08,
        },
        "signal": {
            "signal": "LONG",
            "entry_type": "trend_breakout",
            "last_price": 100.0,
            "stop": 98.0,
            "take_profit": 104.0,
            "atr_pct": 1.0,
        },
    }
    return {
        "action": "OPEN_LONG",
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "quantity": 0.1,
        "leverage": 5,
        "risk_pct": 10,
        "candidate": candidate,
        "signal": candidate["signal"],
    }


def test_exact_order_lineage_captures_features_and_real_costs(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        [index * 60_000, "100", "102", "99", str(100 + index), "1", index * 60_000 + 59_999, str(1000 + index * 10), 3, "0", "0", "0"]
        for index in range(6)
    ]
    write_snapshot(
        {
            "connected": True,
            "updated_at": now,
            "symbols": ["BTCUSDT"],
            "tickers": {},
            "depths": {
                "BTCUSDT": {
                    "spread_pct": 0.02,
                    "depth_notional": 10000,
                    "trade_flow_imbalance": 0.2,
                    "updated_at": now,
                }
            },
            "klines": {
                "BTCUSDT": {
                    "1m": {"row": rows[-1], "history": rows, "updated_at": now}
                }
            },
            "triggers": [],
            "last_error": "",
        }
    )
    init_live_learning_schema()
    with connect() as conn:
        ensure_shadow_tables(conn)

    decision = _decision()
    opportunity_id = record_decision_opportunity(decision)
    assert opportunity_id
    assert decision["candidate"]["opportunity_id"] == opportunity_id
    assert decision["candidate"]["event_id"] == decision["event_id"]

    record_execution_result(
        decision,
        {
            "mode": "live",
            "entry_order": {"orderId": 123, "clientOrderId": "entry-123"},
            "stop_order": {"algoId": 456},
            "take_profit_order": {"algoId": 789},
        },
    )
    record = {
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "open_time": 1_000,
        "close_time": 61_000,
        "open_price": 100.1,
        "close_price": 104.0,
        "quantity": 0.1,
        "open_notional": 10.01,
        "close_notional": 10.4,
        "realized_pnl": 0.39,
        "commission": 0.02,
        "entry_commission": 0.01,
        "close_commission": 0.01,
        "funding_fee": 0.0,
        "net_pnl": 0.37,
        "hold_seconds": 60,
        "trade_count": 2,
        "source": "binance",
        "entry_order_ids": ["123"],
        "exit_order_ids": ["999"],
        "payload": {"fills": []},
    }
    record.update(match_trade_record(record))
    assert record["opportunity_id"] == opportunity_id
    assert record["event_id"] == decision["event_id"]
    assert record["lineage_quality"] == "exact_order_id"
    finalize_trade_lineage(record)
    upsert_trade_records([record])

    quality = training_data_quality()
    assert quality["lineage"]["feature_rows"] == 1
    assert quality["live"]["exact_matches"] == 1
    assert quality["live"]["slippage_rows"] == 1
    assert quality["live"]["events"] == 1
    with connect() as conn:
        stored = dict(
            conn.execute(
                "SELECT id, execution_id, exit_reason, lineage_quality "
                "FROM live_trade_records WHERE symbol = 'BTCUSDT'"
            ).fetchone()
        )
    assert stored["execution_id"] == "binance:123"
    assert stored["exit_reason"] == "take_profit"
    assert stored["lineage_quality"] == "exact_order_id"

    monkeypatch.setattr(
        "app.live_learning.match_trade_record",
        lambda _: {
            "opportunity_id": None,
            "event_id": None,
            "event_group_id": None,
            "execution_id": None,
            "lineage_quality": "unmatched",
            "strategy_family": None,
            "strategy_version": None,
            "strategy_role": None,
        },
    )
    monkeypatch.setattr("app.live_learning.finalize_trade_lineage", lambda _: None)
    raw_record = {
        key: value
        for key, value in record.items()
        if key
        not in {
            "opportunity_id",
            "event_id",
            "event_group_id",
            "execution_id",
            "lineage_quality",
            "entry_slippage_bps",
            "exit_reason",
            "strategy_family",
            "strategy_version",
            "strategy_role",
            "release_id",
        }
    }
    raw_record["entry_order_ids"] = []
    raw_record["exit_order_ids"] = []
    upsert_trade_records([raw_record])

    with connect() as conn:
        preserved = dict(
            conn.execute(
                "SELECT id, execution_id, exit_reason, lineage_quality "
                "FROM live_trade_records WHERE symbol = 'BTCUSDT'"
            ).fetchone()
        )
    assert preserved == stored


def test_user_trade_reconciliation_preserves_order_ids_and_split_commission():
    records = build_trade_records_from_user_trades(
        {
            "BTCUSDT": [
                {
                    "time": 1_000,
                    "positionSide": "LONG",
                    "side": "BUY",
                    "qty": "1",
                    "price": "100",
                    "commission": "-0.04",
                    "realizedPnl": "0",
                    "orderId": 11,
                },
                {
                    "time": 61_000,
                    "positionSide": "LONG",
                    "side": "SELL",
                    "qty": "1",
                    "price": "102",
                    "commission": "-0.05",
                    "realizedPnl": "2",
                    "orderId": 22,
                },
            ]
        }
    )
    assert len(records) == 1
    assert records[0]["entry_order_ids"] == ["11"]
    assert records[0]["exit_order_ids"] == ["22"]
    assert records[0]["entry_commission"] == 0.04
    assert records[0]["close_commission"] == 0.05


def test_match_backfills_execution_id_from_exact_entry_order(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    init_live_learning_schema()
    decision = _decision()
    opportunity_id = record_decision_opportunity(decision)
    with connect() as conn:
        conn.execute(
            """
            UPDATE opportunity_lineage
            SET decision_status = 'EXECUTED', entry_order_id = '321', execution_id = NULL
            WHERE opportunity_id = ?
            """,
            (opportunity_id,),
        )
        conn.commit()

    matched = match_trade_record(
        {
            "symbol": "BTCUSDT",
            "direction": "LONG",
            "open_time": 1_000,
            "entry_order_ids": ["321"],
        }
    )

    assert matched["opportunity_id"] == opportunity_id
    assert matched["execution_id"] == "binance:321"
    assert matched["lineage_quality"] == "exact_order_id"
