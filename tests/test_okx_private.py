from __future__ import annotations

from typing import Any

import pytest

from app.okx_private import OkxPrivateClient, OkxPrivateConfig


class _FakeResponse:
    def __init__(self, payload) -> None:
        self._payload = payload
        self.status_code = 200
        self.text = ""

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


def _config(**kwargs) -> OkxPrivateConfig:
    defaults = dict(
        api_key="key",
        api_secret="secret",
        passphrase="pass",
        dry_run=True,
    )
    defaults.update(kwargs)
    return OkxPrivateConfig(**defaults)


def test_validate_requires_passphrase() -> None:
    with pytest.raises(ValueError):
        OkxPrivateClient(_config(passphrase="")).balance()


def test_balance_sends_signed_headers(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_request(self, method, url, headers=None, data=None, timeout=None):
        captured["method"] = method
        captured["headers"] = headers
        return _FakeResponse({"code": "0", "data": [{"totalEq": "12.3"}]})

    monkeypatch.setattr(
        "app.okx_private.requests.Session.request", fake_request
    )
    client = OkxPrivateClient(_config())
    result = client.balance()
    assert captured["method"] == "GET"
    assert captured["headers"]["OK-ACCESS-KEY"] == "key"
    assert captured["headers"]["OK-ACCESS-PASSPHRASE"] == "pass"
    assert captured["headers"]["OK-ACCESS-SIGN"]
    assert result[0]["totalEq"] == "12.3"


def test_place_order_refuses_in_dry_run() -> None:
    client = OkxPrivateClient(_config())
    with pytest.raises(RuntimeError, match="dry_run"):
        client.place_market_order("BTC-USDT-SWAP", "buy", 0.001)


def test_okx_error_is_raised() -> None:
    def fake_request(self, method, url, headers=None, data=None, timeout=None):
        return _FakeResponse({"code": "50111", "msg": "Invalid OK-ACCESS-KEY"})

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        "app.okx_private.requests.Session.request", fake_request
    )
    try:
        client = OkxPrivateClient(_config())
        with pytest.raises(RuntimeError, match="50111"):
            client.balance()
    finally:
        monkeypatch.undo()
