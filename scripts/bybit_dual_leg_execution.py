from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any

import requests


BYBIT_PUBLIC_BASE = "https://api.bybit.com"


@dataclass(frozen=True)
class PairOrderTemplate:
    symbol: str
    binance_side: str
    bybit_side: str
    notional_per_leg: float
    binance_stop: float | None
    bybit_stop: float | None


class BybitPublicClient:
    """Read-only Bybit market data client (no API key required)."""

    def __init__(self, base_url: str = BYBIT_PUBLIC_BASE, timeout: int = 10) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self.session = requests.Session()

    def ticker(self, symbol: str) -> dict[str, Any]:
        response = self.session.get(
            f"{self.base_url}/v5/market/tickers",
            params={"category": "linear", "symbol": symbol},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("retCode") != 0:
            raise RuntimeError(f"Bybit ticker error: {payload}")
        return payload["result"]["list"][0]

    def last_price(self, symbol: str) -> float:
        return float(self.ticker(symbol)["lastPrice"])


@dataclass(frozen=True)
class ExecutionConfig:
    bybit_api_key: str | None = None
    bybit_api_secret: str | None = None
    dry_run: bool = True
    leverage_per_leg: float = 5.0
    binance_fee_bps: float = 5.0
    bybit_fee_bps: float = 5.5
    slippage_bps: float = 2.0


class BybitPrivateClient:
    """Placeholder for authenticated execution; requires API credentials."""

    def __init__(self, config: ExecutionConfig) -> None:
        if config.dry_run:
            self.enabled = False
            return
        if not config.bybit_api_key or not config.bybit_api_secret:
            raise ValueError(
                "Bybit API key/secret required when dry_run=False"
            )
        self.enabled = True
        self.config = config

    def place_market_order(self, symbol: str, side: str, qty: float) -> dict[str, Any]:
        raise NotImplementedError(
            "Bybit live order placement is intentionally not implemented until "
            "credentials are provided and the strategy is approved."
        )


def build_pair_orders(
    symbol: str,
    direction: int,
    equity: float,
    config: ExecutionConfig,
    premium_pct: float,
) -> PairOrderTemplate:
    """Build the two legs for a premium fade (direction = Binance leg side)."""
    if direction == 1:  # Binance cheap -> long Binance, short Bybit
        binance_side = "Buy"
        bybit_side = "Sell"
    elif direction == -1:  # Binance expensive -> short Binance, long Bybit
        binance_side = "Sell"
        bybit_side = "Buy"
    else:
        raise ValueError("direction must be 1 or -1")
    notional_per_leg = equity * config.leverage_per_leg / 2.0
    stop_distance = abs(premium_pct) * 2.0  # widen 2x -> stop
    return PairOrderTemplate(
        symbol=symbol,
        binance_side=binance_side,
        bybit_side=bybit_side,
        notional_per_leg=round(notional_per_leg, 4),
        binance_stop=round(stop_distance, 4),
        bybit_stop=round(stop_distance, 4),
    )


def compute_premium(binance_price: float, bybit_price: float) -> float:
    if binance_price <= 0 or bybit_price <= 0:
        raise ValueError("prices must be positive")
    return (binance_price / bybit_price - 1.0) * 100.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run scaffold for dual-leg cross-exchange execution. Never places "
            "orders; prints the two-leg plan and current Bybit/Binance prices."
        )
    )
    parser.add_argument("--symbol", default="SOLUSDT")
    parser.add_argument("--binance-price", type=float, required=True)
    parser.add_argument("--bybit-price", type=float, required=True)
    parser.add_argument("--equity", type=float, default=14.0)
    parser.add_argument("--leverage", type=float, default=5.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = ExecutionConfig(leverage_per_leg=args.leverage)
    premium = compute_premium(args.binance_price, args.bybit_price)
    direction = -1 if premium > 0 else 1
    plan = build_pair_orders(
        args.symbol, direction, args.equity, config, premium
    )
    print(
        json.dumps(
            {
                "symbol": args.symbol,
                "premium_pct": round(premium, 4),
                "direction_binance": plan.binance_side,
                "direction_bybit": plan.bybit_side,
                "notional_per_leg_usdt": plan.notional_per_leg,
                "stop_distance_pct": plan.binance_stop,
                "dry_run": True,
                "note": "No orders placed; Bybit live execution requires API credentials.",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
