from app.binance_client import BinanceFuturesClient
from app.binance_rate import BinanceRateLimitError


class FakeHistoryClient(BinanceFuturesClient):
    def __init__(self):
        pass

    def public_get(self, path, params=None):
        end_time = params.get("endTime") if params else None
        limit = params.get("limit", 10)
        if end_time is None:
            last = 100 * 60_000
        else:
            last = int(end_time) // 60_000 * 60_000
        start = max(0, last - (limit - 1) * 60_000)
        return [[ts, 1, 2, 0.5, 1.5, 0, ts + 1, 0, 0, 0, 0, 0] for ts in range(start, last + 1, 60_000)]


def test_klines_history_returns_sorted_deduplicated_rows():
    rows = FakeHistoryClient().klines_history("BTCUSDT", "1m", days=1, warmup=10)
    opens = [row[0] for row in rows]
    assert opens == sorted(set(opens))
    assert len(rows) > 100


def test_signed_request_adds_default_recv_window(monkeypatch):
    captured = {}

    class FakeResponse:
        ok = True
        status_code = 200
        text = "{}"
        headers = {}

        def json(self):
            return {"ok": True}

    def fake_request(method, url, params=None, headers=None, timeout=None):
        captured["params"] = params
        captured["headers"] = headers
        return FakeResponse()

    monkeypatch.setattr("app.binance_client.requests.request", fake_request)

    client = BinanceFuturesClient(api_key="key", api_secret="secret")
    client.signed_request("GET", "/fapi/v2/account")

    assert captured["params"]["recvWindow"] == 10_000
    assert "timestamp" in captured["params"]
    assert "signature" in captured["params"]
    assert captured["headers"]["X-MBX-APIKEY"] == "key"


def test_cancel_algo_order_uses_only_supported_identifier(monkeypatch):
    captured = {}

    def fake_signed_request(self, method, path, params=None):
        captured.update({"method": method, "path": path, "params": params})
        return {"algoId": params["algoId"]}

    monkeypatch.setattr(BinanceFuturesClient, "signed_request", fake_signed_request)

    result = BinanceFuturesClient().cancel_algo_order(12345)

    assert result == {"algoId": 12345}
    assert captured == {
        "method": "DELETE",
        "path": "/fapi/v1/algoOrder",
        "params": {"algoId": 12345},
    }


def test_quantity_algo_stop_can_be_reduce_only_for_safe_bridge(monkeypatch):
    captured = {}

    def fake_signed_request(self, method, path, params=None):
        captured.update({"method": method, "path": path, "params": params})
        return {"algoId": 7}

    monkeypatch.setattr(BinanceFuturesClient, "signed_request", fake_signed_request)

    BinanceFuturesClient().place_algo_order(
        symbol="BTCUSDT",
        side="SELL",
        order_type="STOP_MARKET",
        trigger_price=100,
        close_position=False,
        quantity=0.01,
        reduce_only=True,
    )

    assert captured["path"] == "/fapi/v1/algoOrder"
    assert captured["params"]["quantity"] == 0.01
    assert captured["params"]["reduceOnly"] == "true"
    assert "closePosition" not in captured["params"]


def test_public_request_registers_rate_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))

    class FakeResponse:
        ok = False
        status_code = 429
        text = '{"code":-1003,"msg":"Too many requests"}'
        headers = {"Retry-After": "7"}

        def json(self):
            return {}

    monkeypatch.setattr("app.binance_client.requests.get", lambda *args, **kwargs: FakeResponse())

    client = BinanceFuturesClient()
    try:
        client.public_get("/fapi/v1/time")
        assert False, "expected BinanceRateLimitError"
    except BinanceRateLimitError as exc:
        assert exc.status_code == 429
        assert exc.retry_after and exc.retry_after >= 6
