from __future__ import annotations

import pytest

from app.order_book import LocalOrderBook, OrderBookGap


def test_local_order_book_applies_snapshot_and_sequential_updates():
    book = LocalOrderBook("BTCUSDT")
    book.seed({"lastUpdateId": 100, "bids": [["100", "2"]], "asks": [["101", "3"]]})

    assert book.apply({"U": 99, "u": 101, "pu": 98, "b": [["100", "4"]], "a": [["101", "0"], ["102", "5"]]})
    assert book.apply({"U": 102, "u": 102, "pu": 101, "b": [["99", "1"]], "a": []})
    snapshot = book.snapshot()

    assert snapshot["lastUpdateId"] == 102
    assert snapshot["bids"][0] == ["100.0", "4.0"]
    assert snapshot["asks"][0] == ["102.0", "5.0"]


def test_local_order_book_ignores_old_events_and_rejects_gap():
    book = LocalOrderBook("BTCUSDT")
    book.seed({"lastUpdateId": 100, "bids": [["100", "2"]], "asks": [["101", "3"]]})

    assert book.apply({"U": 90, "u": 99, "pu": 89, "b": [], "a": []}) is False
    assert book.apply({"U": 99, "u": 101, "pu": 98, "b": [], "a": []}) is True
    with pytest.raises(OrderBookGap):
        book.apply({"U": 103, "u": 103, "pu": 102, "b": [], "a": []})
