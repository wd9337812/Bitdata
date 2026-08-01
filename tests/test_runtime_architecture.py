from __future__ import annotations

from app.binance_rate import BinanceRateLimitError, before_request, cache_get, cache_set, cache_status, request_priority
from app.runtime_snapshot import read_runtime_snapshot, update_runtime_snapshot
from app.runner import background_loop_seconds
from app.scanner import scan_growth_candidates
from app.shadow_trading import ensure_shadow_tables
from app.telemetry import compact_decision, connect, maintain_telemetry, record_strategy_run


def test_sqlite_cache_round_trip_without_monolithic_json(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))

    cache_set("kline:TEST", {"bars": [[1, 2, 3]]})
    entry = cache_get("kline:TEST", 60)

    assert entry is not None
    assert entry.value["bars"][0][1] == 2
    assert cache_status()["backend"] == "sqlite_wal"
    assert (tmp_path / "binance_cache.db").exists()
    assert not (tmp_path / "binance_cache.json").exists()


def test_background_rate_priority_preserves_realtime_budget(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    with request_priority("background"):
        before_request(330, budget_per_minute=600)
        try:
            before_request(1, budget_per_minute=600)
        except BinanceRateLimitError as exc:
            assert 0 < float(exc.retry_after or 0) <= 5
        else:
            raise AssertionError("background request should preserve realtime reserve")
    with request_priority("realtime"):
        before_request(1, budget_per_minute=600)


def test_background_scan_has_independent_minimum_interval():
    assert background_loop_seconds({"background_scan_min_interval_seconds": 30}, {"loop_seconds": 5}) == 30
    assert background_loop_seconds({"background_scan_min_interval_seconds": 30}, {"loop_seconds": 60}) == 60


def test_runtime_snapshot_is_atomic_and_reports_age(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))

    update_runtime_snapshot(channel="fast_lane", fast_lane={"elapsed_seconds": 2.3})
    snapshot = read_runtime_snapshot()

    assert snapshot["channel"] == "fast_lane"
    assert snapshot["fast_lane"]["elapsed_seconds"] == 2.3
    assert snapshot["age_seconds"] is not None


def test_compact_decision_drops_large_universe_but_keeps_learning_metrics():
    decision = {
        "action": "WAIT",
        "scan": {
            "ranked_symbols": [f"S{i}USDT" for i in range(600)],
            "recalled_symbols": [f"S{i}USDT" for i in range(600)],
            "candidates": [
                {
                    "symbol": "TESTUSDT",
                    "opportunity_v3": {"tier": "A+"},
                    "market_structure": {"schema_version": "market-structure-v1", "market_regime": "broad_down"},
                    "opportunity_v4": {"strategy_version": "v4.2", "admission_lane": "shadow_only"},
                    "coarse": {"large": "x" * 1000},
                    "backtests": {"2": {"trades": 8, "win_rate": 50, "net_pct": 2, "profit_factor": 1.2, "raw": "x" * 1000}},
                }
            ],
        },
    }

    compact = compact_decision(decision)

    assert "ranked_symbols" not in compact["scan"]
    assert "recalled_symbols" not in compact["scan"]
    assert "coarse" not in compact["scan"]["candidates"][0]
    assert "opportunity_v3" not in compact["scan"]["candidates"][0]
    assert compact["scan"]["candidates"][0]["opportunity_v4"]["strategy_version"] == "v4.2"
    assert compact["scan"]["candidates"][0]["backtests"]["2"]["profit_factor"] == 1.2


def test_telemetry_database_uses_wal_and_indexes(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))

    with connect() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}

    assert mode.lower() == "wal"
    assert "idx_strategy_runs_symbol_ts" in indexes


def test_telemetry_maintenance_prunes_only_old_scan_and_closed_shadow_rows(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    with connect() as conn:
        ensure_shadow_tables(conn)
        conn.execute(
            "INSERT INTO strategy_runs (ts, action, payload) VALUES ('2020-01-01T00:00:00+00:00', 'WAIT', '{}')"
        )
        conn.execute(
            """
            INSERT INTO shadow_trades (
                dedupe_key, opened_at, closed_at, symbol, direction, status, entry, stop,
                take_profit, last_price, notional, estimated_cost, expires_at, strategy_family
            ) VALUES ('slow-research', '2020-01-01T00:00:00+00:00', '2020-01-06T00:00:00+00:00',
                      'SLOWUSDT', 'LONG', 'CLOSED', 1, 0.9, 1.1, 1, 20, 0.02,
                      '2020-01-06T00:00:00+00:00', 'adaptive_30d_momentum')
            """
        )
        conn.execute(
            """
            INSERT INTO shadow_trades (
                dedupe_key, opened_at, closed_at, symbol, direction, status, entry, stop,
                take_profit, last_price, notional, estimated_cost, expires_at
            ) VALUES ('old', '2020-01-01T00:00:00+00:00', '2020-01-01T01:00:00+00:00',
                      'OLDUSDT', 'LONG', 'CLOSED', 1, 0.9, 1.1, 1, 20, 0.02,
                      '2020-01-01T02:00:00+00:00')
            """
        )
        conn.execute(
            """
            INSERT INTO shadow_trades (
                dedupe_key, opened_at, symbol, direction, status, entry, stop,
                take_profit, last_price, notional, estimated_cost, expires_at
            ) VALUES ('open', '2020-01-01T00:00:00+00:00', 'OPENUSDT', 'LONG', 'OPEN',
                      1, 0.9, 1.1, 1, 20, 0.02, '2099-01-01T00:00:00+00:00')
            """
        )
        conn.commit()

    result = maintain_telemetry(30, strategy_run_retention_days=7, shadow_trade_retention_days=14)

    assert result["strategy_runs_deleted"] == 1
    assert result["shadow_trades_deleted"] == 1
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM shadow_trades WHERE status = 'OPEN'").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM shadow_trades WHERE strategy_family = 'adaptive_30d_momentum'"
        ).fetchone()[0] == 1


def test_wait_strategy_run_throttle_keeps_first_row_only(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    decision = {"action": "WAIT", "reason": "no_candidate_passed", "scan": {"mode": {"mode": "extreme_sprint"}}}

    first = record_strategy_run({"stage": "growth"}, {"equity": 20}, decision, throttle_seconds=30)
    second = record_strategy_run({"stage": "growth"}, {"equity": 20}, decision, throttle_seconds=30)

    assert first is True
    assert second is False
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM strategy_runs").fetchone()[0] == 1


def test_fast_lane_scans_only_explicit_symbols(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr("app.scanner.discover_coin_symbols", lambda *_: (_ for _ in ()).throw(AssertionError("full discovery called")))
    monkeypatch.setattr("app.scanner.read_opportunities", lambda **_: [])
    monkeypatch.setattr("app.scanner.stream_triggers", lambda **_: [])

    class Client:
        def ticker_24h(self, symbols):
            return [
                {"symbol": symbol, "lastPrice": "1", "priceChangePercent": "2", "quoteVolume": "100000000"}
                for symbol in symbols
            ]

        def premium_index(self, symbols):
            return [{"symbol": symbol, "lastFundingRate": "0"} for symbol in symbols]

        def klines_history(self, *args, **kwargs):
            raise RuntimeError("stop after proving symbol selection")

    result = scan_growth_candidates(
        Client(),
        {
            "growth_mode": "extreme_sprint",
            "extreme_sprint_enabled": True,
            "extreme_sprint_confirmation": "ENABLE_EXTREME_SPRINT",
            "auto_risk_by_equity": False,
            "opportunity_queue_enabled": True,
            "websocket_trigger_enabled": True,
            "fast_lane_max_symbols": 3,
        },
        {"equity": 50, "positions": []},
        symbols_override=["AAAUSDT", "BBBUSDT"],
        fast_lane=True,
    )

    assert result["recalled_symbols"] == ["AAAUSDT", "BBBUSDT"]
    assert result["funnel"]["channel"] == "fast_lane"
