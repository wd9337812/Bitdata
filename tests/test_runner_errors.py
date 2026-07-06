from __future__ import annotations

from app.runner import is_min_notional_rejection


def test_min_notional_rejection_is_nonfatal_exchange_rejection():
    exc = RuntimeError('Binance signed API 400: {"code":-4164,"msg":"Order\'s notional must be no smaller than 5"}')

    assert is_min_notional_rejection(exc) is True
    assert is_min_notional_rejection(RuntimeError("timestamp outside recvWindow")) is False

