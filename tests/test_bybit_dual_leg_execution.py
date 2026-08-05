from __future__ import annotations

import pytest

from scripts.bybit_dual_leg_execution import (
    BybitPrivateClient,
    ExecutionConfig,
    build_pair_orders,
    compute_premium,
    execute_okx_leg,
    okx_instrument,
)
from app.okx_private import OkxPrivateConfig


def test_compute_premium() -> None:
    assert compute_premium(101.0, 100.0) == pytest.approx(1.0)
    assert compute_premium(100.0, 101.0) == pytest.approx(-0.990099, abs=1e-5)


def test_build_pair_orders_long_binance_when_cheap() -> None:
    config = ExecutionConfig(leverage_per_leg=5.0)
    plan = build_pair_orders(
        "SOLUSDT", direction=1, equity=14.0, config=config, premium_pct=-0.2
    )
    assert plan.binance_side == "Buy"
    assert plan.bybit_side == "Sell"
    assert plan.notional_per_leg == pytest.approx(35.0)
    assert plan.binance_stop == pytest.approx(0.4)


def test_private_client_requires_credentials() -> None:
    with pytest.raises(ValueError):
        BybitPrivateClient(ExecutionConfig(dry_run=False))
    assert BybitPrivateClient(ExecutionConfig(dry_run=True)).enabled is False


def test_okx_instrument_mapping() -> None:
    assert okx_instrument("SOLUSDT") == "SOL-USDT-SWAP"
    assert okx_instrument("BTCUSDT") == "BTC-USDT-SWAP"


def test_execute_okx_leg_refuses_dry_run() -> None:
    plan = build_pair_orders(
        "SOLUSDT", direction=-1, equity=14.0, config=ExecutionConfig(), premium_pct=0.2
    )
    with pytest.raises(RuntimeError, match="dry_run"):
        execute_okx_leg(
            plan,
            second_price=130.0,
            okx_config=OkxPrivateConfig(dry_run=True),
        )


def test_execute_okx_leg_places_order() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls = []

        def place_market_order(self, instrument, side, qty):
            self.calls.append((instrument, side, qty))
            return {"ordId": "123"}

    plan = build_pair_orders(
        "SOLUSDT", direction=-1, equity=14.0, config=ExecutionConfig(), premium_pct=0.2
    )
    fake = FakeClient()
    result = execute_okx_leg(
        plan,
        second_price=130.0,
        okx_config=OkxPrivateConfig(dry_run=False),
        client=fake,
    )
    assert result["ok"] is True
    assert fake.calls[0][0] == "SOL-USDT-SWAP"
    assert fake.calls[0][1] == "buy"
    assert fake.calls[0][2] == pytest.approx(round(35.0 / 130.0, 6), abs=1e-9)
