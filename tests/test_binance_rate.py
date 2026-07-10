from __future__ import annotations

import pytest

from app.binance_rate import (
    BinanceRateLimitError,
    before_request,
    configure_exchange_limits,
    rate_status,
    request_priority,
)


def test_exchange_info_limits_drive_internal_priority_budgets(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    configure_exchange_limits(
        {
            "rateLimits": [
                {"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 1000},
                {"rateLimitType": "ORDERS", "interval": "SECOND", "intervalNum": 10, "limit": 300},
                {"rateLimitType": "ORDERS", "interval": "MINUTE", "intervalNum": 1, "limit": 1200},
            ]
        }
    )

    status = rate_status()

    assert status["budgets"]["advertised"] == 1000
    assert status["budgets"]["background"] == 400
    assert status["budgets"]["normal"] == 550
    assert status["budgets"]["realtime"] == 750
    assert status["budgets"]["critical"] == 900


def test_background_requests_leave_capacity_for_realtime(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    configure_exchange_limits(
        {"rateLimits": [{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 100}]}
    )

    with request_priority("background"):
        before_request(40)
        with pytest.raises(BinanceRateLimitError):
            before_request(1)
    with request_priority("realtime"):
        before_request(1)


def test_order_counter_stops_before_exchange_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    configure_exchange_limits(
        {
            "rateLimits": [
                {"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 1000},
                {"rateLimitType": "ORDERS", "interval": "SECOND", "intervalNum": 10, "limit": 10},
                {"rateLimitType": "ORDERS", "interval": "MINUTE", "intervalNum": 1, "limit": 100},
            ]
        }
    )

    with request_priority("critical"):
        before_request(1, order_count=9)
        with pytest.raises(BinanceRateLimitError, match="订单频率"):
            before_request(1, order_count=1)
