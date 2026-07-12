from __future__ import annotations

import time

from app import main
from app.binance_rate import BinanceRateLimitError


def test_binance_health_reuses_sixty_second_cache(monkeypatch):
    calls = {"count": 0}

    class Client:
        def server_time(self):
            calls["count"] += 1
            return {"serverTime": int(time.time() * 1000)}

    main._BINANCE_HEALTH_CACHE.clear()
    monkeypatch.setattr(main, "client_from_config", lambda: Client())

    first = main.binance_health()
    second = main.binance_health()

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["cached"] is True
    assert calls["count"] == 1


def test_binance_health_caches_stream_fallback_when_budget_is_reserved(monkeypatch):
    class Client:
        def server_time(self):
            raise BinanceRateLimitError("reserved", retry_after=1, status_code=429)

    main._BINANCE_HEALTH_CACHE.clear()
    monkeypatch.setattr(main, "client_from_config", lambda: Client())

    first = main.binance_health()
    second = main.binance_health()

    assert first["ok"] is True
    assert first["deferred"] is True
    assert second["cached"] is True
