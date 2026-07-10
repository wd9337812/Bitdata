from __future__ import annotations

from app.binance_rate import BinanceRateLimitError, before_request, cache_get, cache_set, cache_status, request_priority
from app.runtime_snapshot import read_runtime_snapshot, update_runtime_snapshot
from app.scanner import scan_growth_candidates
from app.telemetry import compact_decision, connect


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
            assert exc.retry_after == 5
        else:
            raise AssertionError("background request should preserve realtime reserve")
    with request_priority("realtime"):
        before_request(1, budget_per_minute=600)


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
    assert compact["scan"]["candidates"][0]["backtests"]["2"]["profit_factor"] == 1.2


def test_telemetry_database_uses_wal_and_indexes(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))

    with connect() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}

    assert mode.lower() == "wal"
    assert "idx_strategy_runs_symbol_ts" in indexes


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
