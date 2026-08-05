from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.registration_free_market import OkxPublicClient  # noqa: E402

DEFAULT_STATE = ROOT / "data" / "research" / "s0_okx_pair_forward_shadow" / "state.json"
DEFAULT_RECORDS = ROOT / "data" / "research" / "s0_okx_pair_forward_shadow" / "records.jsonl"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT", "ADAUSDT", "BNBUSDT", "LINKUSDT")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Forward shadow for the Binance-OKX two-leg dislocation strategy. "
            "Uses only public market data; never places orders."
        )
    )
    parser.add_argument("--symbols", nargs="+", default=list(SYMBOLS))
    parser.add_argument("--threshold-pct", type=float, default=0.20)
    parser.add_argument("--z-threshold", type=float, default=3.0)
    parser.add_argument("--window-minutes", type=int, default=60)
    parser.add_argument("--min-samples", type=int, default=30)
    parser.add_argument("--revert-ratio", type=float, default=0.5)
    parser.add_argument("--stop-ratio", type=float, default=2.0)
    parser.add_argument("--max-hold-minutes", type=int, default=60)
    parser.add_argument("--leverage-per-leg", type=float, default=5.0)
    parser.add_argument("--cost-bps-per-leg", type=float, default=7.0)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop-interval", type=float, default=30.0)
    return parser.parse_args()


def fetch_prices(symbols: list[str]) -> dict[str, dict[str, float]]:
    bin_prices: dict[str, float] = {}
    for symbol in symbols:
        response = requests.get(
            "https://fapi.binance.com/fapi/v1/ticker/price",
            params={"symbol": symbol},
            timeout=15,
        )
        response.raise_for_status()
        item = response.json()
        bin_prices[symbol] = float(item["price"])
    okx = OkxPublicClient()
    okx_prices = {symbol: okx.last_price(symbol) for symbol in symbols}
    return {
        symbol: {
            "binance": bin_prices.get(symbol, 0.0),
            "okx": okx_prices.get(symbol, 0.0),
        }
        for symbol in symbols
    }


def evaluate(
    state: dict[str, Any],
    prices: dict[str, dict[str, float]],
    now_ms: int,
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    history = state.setdefault("history", {})
    window_ms = args.window_minutes * 60_000
    for symbol in args.symbols:
        bin_price = prices.get(symbol, {}).get("binance", 0.0)
        okx_price = prices.get(symbol, {}).get("okx", 0.0)
        if bin_price <= 0 or okx_price <= 0:
            continue
        premium = (bin_price / okx_price - 1.0) * 100.0
        series = [
            item for item in history.get(symbol, [])
            if item[0] >= now_ms - window_ms
        ]
        series.append([now_ms, premium])
        history[symbol] = series[-2000:]
        if len(series) >= args.min_samples:
            values = [item[1] for item in series]
            mean = sum(values) / len(values)
            variance = sum((value - mean) ** 2 for value in values) / len(values)
            std = math.sqrt(variance) if variance > 0 else 0.0
            z = (premium - mean) / std if std > 0 else 0.0
        else:
            z = 0.0
        state.setdefault("last_premium", {})[symbol] = {
            "premium": premium,
            "z": z,
        }
        open_position = state.get("open_position")
        if open_position is not None:
            symbol_pos = open_position["symbol"]
            if symbol_pos == symbol:
                entry_premium = open_position["entry_premium"]
                direction = open_position["direction"]
                hold_minutes = (now_ms - open_position["entry_ms"]) / 60_000
                outcome = None
                if direction == -1:
                    if premium >= entry_premium * args.stop_ratio:
                        outcome = "STOP"
                    elif premium <= entry_premium * args.revert_ratio:
                        outcome = "REVERT"
                else:
                    if premium <= entry_premium * args.stop_ratio:
                        outcome = "STOP"
                    elif premium >= entry_premium * args.revert_ratio:
                        outcome = "REVERT"
                if outcome is None and hold_minutes >= args.max_hold_minutes:
                    outcome = "TIME"
                if outcome is not None:
                    bin_leg = (bin_price / open_position["binance_entry"] - 1.0) * direction
                    okx_leg = (okx_price / open_position["okx_entry"] - 1.0) * -direction
                    cost = args.cost_bps_per_leg * 2 / 10_000 * args.leverage_per_leg / 2 * 100
                    pnl = (bin_leg + okx_leg) * 100 * args.leverage_per_leg / 2 - cost
                    record = {
                        "symbol": symbol,
                        "direction": direction,
                        "entry_ms": open_position["entry_ms"],
                        "exit_ms": now_ms,
                        "entry_premium": entry_premium,
                        "exit_premium": premium,
                        "outcome": outcome,
                        "pnl_equity_pct": round(pnl, 6),
                        "hold_minutes": round(hold_minutes, 2),
                    }
                    events.append(record)
                    state["open_position"] = None
        elif (
            abs(premium) >= args.threshold_pct
            and abs(z) >= args.z_threshold
        ):
            direction = -1 if premium > 0 else 1
            state["open_position"] = {
                "symbol": symbol,
                "direction": direction,
                "entry_ms": now_ms,
                "entry_premium": premium,
                "binance_entry": bin_price,
                "okx_entry": okx_price,
            }
            events.append(
                {
                    "type": "OPEN",
                    "symbol": symbol,
                    "direction": direction,
                    "entry_ms": now_ms,
                    "premium": premium,
                    "binance": bin_price,
                    "okx": okx_price,
                }
            )
    return events


def main() -> None:
    args = parse_args()
    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.records.parent.mkdir(parents=True, exist_ok=True)
    state: dict[str, Any] = {}
    if args.state.exists():
        state = json.loads(args.state.read_text(encoding="utf-8"))
    if args.once:
        prices = fetch_prices(args.symbols)
        events = evaluate(state, prices, int(time.time() * 1000), args)
        args.state.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        with args.records.open("a", encoding="utf-8") as handle:
            for event in events:
                if event.get("type") == "OPEN":
                    print(f"OPEN {event['symbol']} dir={event['direction']} premium={event['premium']:.3f}%", flush=True)
                else:
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                    print(
                        f"CLOSE {event['symbol']} {event['outcome']} "
                        f"pnl={event['pnl_equity_pct']:.3f}%",
                        flush=True,
                    )
        return
    while True:
        try:
            prices = fetch_prices(args.symbols)
            events = evaluate(state, prices, int(time.time() * 1000), args)
            args.state.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            with args.records.open("a", encoding="utf-8") as handle:
                for event in events:
                    if event.get("type") == "OPEN":
                        print(f"OPEN {event['symbol']} dir={event['direction']} premium={event['premium']:.3f}%", flush=True)
                    else:
                        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                        print(f"CLOSE {event['symbol']} {event['outcome']} pnl={event['pnl_equity_pct']:.3f}%", flush=True)
        except Exception as exc:
            print(f"ERROR {exc}", flush=True)
        time.sleep(args.loop_interval)


if __name__ == "__main__":
    main()
