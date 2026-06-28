from app.binance_client import BinanceFuturesClient


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
