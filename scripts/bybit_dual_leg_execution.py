from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.registration_free_market import client_for  # noqa: E402
from app.okx_private import OkxPrivateClient, OkxPrivateConfig  # noqa: E402


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


def okx_instrument(symbol: str) -> str:
    base = symbol.removesuffix("USDT")
    return f"{base}-USDT-SWAP"


def execute_okx_leg(
    plan: PairOrderTemplate,
    second_price: float,
    okx_config: OkxPrivateConfig,
    client: OkxPrivateClient | None = None,
) -> dict[str, Any]:
    """Place the OKX leg. Raises in dry-run mode (no live orders)."""
    if okx_config.dry_run:
        raise RuntimeError("dry_run=True: refusing to place a live OKX order")
    qty = plan.notional_per_leg / second_price
    executor = client or OkxPrivateClient(okx_config)
    result = executor.place_market_order(
        okx_instrument(plan.symbol),
        plan.bybit_side.lower(),
        round(qty, 6),
    )
    return {"ok": True, "okx_order": result}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run scaffold for dual-leg cross-exchange execution. Never places "
            "orders; prints the two-leg plan and current Bybit/Binance prices."
        )
    )
    parser.add_argument("--symbol", default="SOLUSDT")
    parser.add_argument(
        "--venue",
        default="okx",
        choices=("okx", "gate", "kucoin", "hyperliquid"),
        help="Registration-free second-venue data source.",
    )
    parser.add_argument("--binance-price", type=float, default=None)
    parser.add_argument("--second-price", type=float, default=None)
    parser.add_argument("--equity", type=float, default=14.0)
    parser.add_argument("--leverage", type=float, default=5.0)
    parser.add_argument("--okx-api-key", default=None)
    parser.add_argument("--okx-api-secret", default=None)
    parser.add_argument("--okx-passphrase", default=None)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Allow live OKX orders (dangerous; requires all three credentials).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = ExecutionConfig(leverage_per_leg=args.leverage)
    second_price = args.second_price
    if second_price is None:
        second_price = client_for(args.venue).last_price(args.symbol)
    if args.binance_price is None:
        raise SystemExit("--binance-price is required (or provide both prices)")
    premium = compute_premium(args.binance_price, second_price)
    direction = -1 if premium > 0 else 1
    plan = build_pair_orders(
        args.symbol, direction, args.equity, config, premium
    )
    output: dict[str, Any] = {
        "symbol": args.symbol,
        "premium_pct": round(premium, 4),
        "direction_binance": plan.binance_side,
        "direction_second_venue": plan.bybit_side,
        "second_venue": args.venue,
        "second_price": second_price,
        "notional_per_leg_usdt": plan.notional_per_leg,
        "stop_distance_pct": plan.binance_stop,
    }
    if args.venue == "okx" and args.live:
        okx_config = OkxPrivateConfig(
            api_key=args.okx_api_key or "",
            api_secret=args.okx_api_secret or "",
            passphrase=args.okx_passphrase or "",
            dry_run=False,
        )
        result = execute_okx_leg(plan, second_price, okx_config)
        output.update({"dry_run": False, "okx_leg": result})
        output["note"] = (
            "OKX leg submitted; Binance leg must be submitted by the live bot."
        )
    else:
        output.update(
            {
                "dry_run": True,
                "note": (
                    "No orders placed. Use --live with OKX credentials to enable "
                    "the OKX leg; Binance leg still requires the live bot."
                ),
            }
        )
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
