from __future__ import annotations

import pytest

from scripts.bybit_dual_leg_execution import (
    BybitPrivateClient,
    ExecutionConfig,
    build_pair_orders,
    compute_premium,
)


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
