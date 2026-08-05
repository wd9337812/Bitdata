from __future__ import annotations

import pytest

from app.registration_free_market import (
    GatePublicClient,
    HyperliquidPublicClient,
    KuCoinFuturesPublicClient,
    OkxPublicClient,
    client_for,
)


class _FakeResponse:
    def __init__(self, payload) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


def test_okx_last_price_anonymous(monkeypatch) -> None:
    def fake_get(url, params=None, timeout=None):
        assert params["instId"] == "BTC-USDT-SWAP"
        return _FakeResponse({"code": "0", "data": [{"last": "65000.5"}]})

    monkeypatch.setattr("app.registration_free_market.requests.get", fake_get)
    assert OkxPublicClient().last_price("BTCUSDT") == pytest.approx(65000.5)


def test_gate_last_price_anonymous(monkeypatch) -> None:
    def fake_get(url, params=None, timeout=None):
        assert params["contract"] == "SOL_USDT"
        return _FakeResponse([{"last": "130.25"}])

    monkeypatch.setattr("app.registration_free_market.requests.get", fake_get)
    assert GatePublicClient().last_price("SOLUSDT") == pytest.approx(130.25)


def test_kucoin_last_price_anonymous(monkeypatch) -> None:
    def fake_get(url, params=None, timeout=None):
        assert params["symbol"] == "XBTUSDTM"
        return _FakeResponse({"data": {"last": "65000"}})

    monkeypatch.setattr("app.registration_free_market.requests.get", fake_get)
    assert KuCoinFuturesPublicClient().last_price("BTCUSDT") == pytest.approx(65000)


def test_client_for_unsupported_venue() -> None:
    with pytest.raises(ValueError):
        client_for("bybit")
    assert isinstance(client_for("okx"), OkxPublicClient)


def test_hyperliquid_last_price_anonymous(monkeypatch) -> None:
    def fake_post(url, json=None, timeout=None):
        assert json["type"] == "allMids"
        return _FakeResponse({"BTC": "64000.5"})

    monkeypatch.setattr("app.registration_free_market.requests.post", fake_post)
    assert HyperliquidPublicClient().last_price("BTCUSDT") == pytest.approx(64000.5)
