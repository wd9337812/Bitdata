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

DEFAULT_STATE = ROOT / "data" / "research" / "s0_forward_event_monitor" / "state.json"
DEFAULT_RECORDS = ROOT / "data" / "research" / "s0_forward_event_monitor" / "records.jsonl"
FALLBACK_SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT", "BNBUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT", "BCHUSDT",
    "XLMUSDT", "UNIUSDT", "AAVEUSDT", "FILUSDT", "NEARUSDT", "HBARUSDT",
    "ARBUSDT", "SUIUSDT", "OPUSDT", "INJUSDT", "WLDUSDT", "PEPEUSDT",
    "SHIBUSDT", "TRXUSDT", "ATOMUSDT", "ETCUSDT", "APTUSDT", "TIAUSDT",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Forward event shadow: records funding-extreme, volume-breakout and "
            "BTC-impulse events with forward returns. Public data only; no orders."
        )
    )
    parser.add_argument("--symbols", nargs="+", default=None)
    parser.add_argument("--funding-threshold-pct", type=float, default=0.05)
    parser.add_argument("--funding-z", type=float, default=2.0)
    parser.add_argument("--vol-z", type=float, default=3.0)
    parser.add_argument("--btc-impulse-z", type=float, default=3.0)
    parser.add_argument("--horizon-hours", type=float, default=24.0)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop-interval", type=float, default=300.0)
    return parser.parse_args()


def _get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    response = requests.get(url, params=params, timeout=20)
    response.raise_for_status()
    return response.json()


def z_score(values: list[float], value: float) -> float:
    if len(values) < 20:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((item - mean) ** 2 for item in values) / len(values)
    std = math.sqrt(variance) if variance > 0 else 0.0
    return (value - mean) / std if std > 0 else 0.0


def fetch_funding(symbol: str, limit: int = 500) -> list[dict[str, Any]]:
    return _get_json(
        "https://fapi.binance.com/fapi/v1/fundingRate",
        {"symbol": symbol, "limit": str(limit)},
    )


def fetch_klines(symbol: str, interval: str, limit: int = 200) -> list[dict[str, Any]]:
    return _get_json(
        "https://fapi.binance.com/fapi/v1/klines",
        {"symbol": symbol, "interval": interval, "limit": str(limit)},
    )


def fetch_price(symbol: str) -> float:
    item = _get_json(
        "https://fapi.binance.com/fapi/v1/ticker/price",
        {"symbol": symbol},
    )
    return float(item["price"])


def evaluate_funding(state: dict[str, Any], symbol: str, args: argparse.Namespace) -> dict[str, Any] | None:
    try:
        rows = fetch_funding(symbol)
    except Exception:
        return None
    if len(rows) < 30:
        return None
    rates = [float(row["fundingRate"]) * 100.0 for row in rows]
    latest = rates[-1]
    z = z_score(rates[:-1], latest)
    if abs(latest) >= args.funding_threshold_pct and abs(z) >= args.funding_z:
        return {
            "type": "funding_extreme",
            "symbol": symbol,
            "direction": -1 if latest > 0 else 1,
            "strength": round(abs(latest), 4),
            "z": round(z, 3),
            "ts": int(rows[-1]["fundingTime"]),
        }
    return None


def evaluate_volume(state: dict[str, Any], symbol: str, args: argparse.Namespace) -> dict[str, Any] | None:
    try:
        klines = fetch_klines(symbol, "1h", 200)
    except Exception:
        return None
    if len(klines) < 60:
        return None
    volumes = [float(item[5]) for item in klines[:-1]]
    latest_volume = float(klines[-1][5])
    latest_close = float(klines[-1][4])
    latest_open = float(klines[-1][1])
    z = z_score(volumes, latest_volume)
    if z >= args.vol_z and latest_close > latest_open:
        return {
            "type": "volume_breakout",
            "symbol": symbol,
            "direction": 1,
            "strength": round(z, 3),
            "z": round(z, 3),
            "ts": int(klines[-1][0]),
        }
    if z >= args.vol_z and latest_close < latest_open:
        return {
            "type": "volume_breakout",
            "symbol": symbol,
            "direction": -1,
            "strength": round(z, 3),
            "z": round(z, 3),
            "ts": int(klines[-1][0]),
        }
    return None


def evaluate_btc_impulse(state: dict[str, Any], args: argparse.Namespace) -> dict[str, Any] | None:
    try:
        klines = fetch_klines("BTCUSDT", "1h", 300)
    except Exception:
        return None
    if len(klines) < 60:
        return None
    closes = [float(item[4]) for item in klines]
    rets = [
        (closes[i] / closes[i - 4] - 1.0) * 100.0
        for i in range(4, len(closes))
    ]
    latest = rets[-1]
    z = z_score(rets[:-1], latest)
    if abs(z) >= args.btc_impulse_z:
        return {
            "type": "btc_impulse",
            "symbol": "BTCUSDT",
            "direction": 1 if latest > 0 else -1,
            "strength": round(abs(latest), 3),
            "z": round(z, 3),
            "ts": int(klines[-1][0]),
        }
    return None


def select_30d_momentum(args: argparse.Namespace) -> dict[str, Any] | None:
    """Daily 30d cross-sectional momentum selector (public API, frozen rules)."""
    tickers = _get_json("https://fapi.binance.com/fapi/v1/ticker/24hr")
    candidates = [
        item
        for item in tickers
        if str(item["symbol"]).endswith("USDT")
        and str(item["symbol"]) not in ("BTCUSDT", "ETHUSDT")
        and float(item.get("quoteVolume", 0.0)) >= 20_000_000.0
    ]
    candidates.sort(key=lambda item: float(item["quoteVolume"]), reverse=True)
    candidates = candidates[:80]
    returns: dict[str, float] = {}
    for item in candidates:
        symbol = str(item["symbol"])
        try:
            daily = fetch_klines(symbol, "1d", 32)
        except Exception:
            continue
        if len(daily) < 32:
            continue
        returns[symbol] = float(daily[-1][4]) / float(daily[-31][4]) - 1.0
    if len(returns) < 60:
        return None
    btc_daily = fetch_klines("BTCUSDT", "1d", 32)
    if len(btc_daily) < 32:
        return None
    btc_return = float(btc_daily[-1][4]) / float(btc_daily[-31][4]) - 1.0
    values = list(returns.values())
    values.sort()
    median = values[len(values) // 2] if values else 0.0
    breadth = median
    if btc_return * breadth <= 0 or abs(breadth) < 0.02 or abs(breadth) > 0.10:
        return None
    direction = 1 if breadth > 0 else -1
    aligned = [
        (symbol, ret)
        for symbol, ret in returns.items()
        if (ret > 0) == (direction > 0)
    ]
    if not aligned:
        return None
    symbol, ret30 = max(aligned, key=lambda item: abs(item[1]))
    hourly = fetch_klines(symbol, "1h", 25)
    if len(hourly) < 25:
        return None
    ret24 = float(hourly[-1][4]) / float(hourly[-24][4]) - 1.0
    ret6 = float(hourly[-1][4]) / float(hourly[-6][4]) - 1.0
    acceleration = (ret6 * ret24 > 0) and abs(ret6) >= 0.005
    breadth_confirmation = (breadth * direction > 0) and abs(breadth) >= 0.005
    return {
        "symbol": symbol,
        "direction": direction,
        "ret30": ret30,
        "breadth": breadth,
        "btc_return": btc_return,
        "acceleration": acceleration,
        "breadth_confirmation": breadth_confirmation,
        "confirmed": acceleration and breadth_confirmation,
        "universe": len(returns),
        "ts": int(time.time() * 1000),
        "entry_price": float(
            next(item["lastPrice"] for item in tickers if item["symbol"] == symbol)
        ),
    }


def evaluate_once(state: dict[str, Any], args: argparse.Namespace) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    symbols = args.symbols or list(FALLBACK_SYMBOLS)
    for symbol in symbols:
        funding = evaluate_funding(state, symbol, args)
        if funding:
            events.append(funding)
        volume = evaluate_volume(state, symbol, args)
        if volume:
            events.append(volume)
    btc = evaluate_btc_impulse(state, args)
    if btc:
        events.append(btc)
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if state.get("momentum_last_day") != today:
        momentum = select_30d_momentum(args)
        if momentum is not None:
            events.append(
                {
                    "type": "momentum_confirmed" if momentum["confirmed"] else "momentum_unconfirmed",
                    "symbol": momentum["symbol"],
                    "direction": momentum["direction"],
                    "strength": round(abs(momentum["ret30"]) * 100, 3),
                    "z": round(momentum["breadth"] * 100, 3),
                    "ts": momentum["ts"],
                    "confirmed": momentum["confirmed"],
                    "ret30": momentum["ret30"],
                    "breadth": momentum["breadth"],
                    "acceleration": momentum["acceleration"],
                    "breadth_confirmation": momentum["breadth_confirmation"],
                    "universe": momentum["universe"],
                    "entry_price": momentum["entry_price"],
                    "horizon_hours": 120.0,
                }
            )
        state["momentum_last_day"] = today
    return events


def close_expired(
    state: dict[str, Any],
    args: argparse.Namespace,
    records_path: Path,
) -> list[dict[str, Any]]:
    closed: list[dict[str, Any]] = []
    open_events = state.setdefault("open_events", [])
    remaining: list[dict[str, Any]] = []
    for event in open_events:
        age_hours = (time.time() * 1000 - event["ts"]) / 3_600_000
        horizon_hours = float(event.get("horizon_hours", args.horizon_hours))
        if age_hours < horizon_hours:
            remaining.append(event)
            continue
        try:
            current = fetch_price(event["symbol"])
            klines = fetch_klines(event["symbol"], "1h", 200)
        except Exception:
            remaining.append(event)
            continue
        direction = event["direction"]
        raw_return = (current / event["entry_price"] - 1.0) * direction * 100.0
        mfe = 0.0
        for item in klines:
            high = float(item[2])
            low = float(item[3])
            favorable = max(high / event["entry_price"] - 1.0, event["entry_price"] / low - 1.0) * 100.0
            mfe = max(mfe, favorable)
        record = {
            **event,
            "exit_ts": int(time.time() * 1000),
            "raw_return_pct": round(raw_return, 4),
            "mfe_pct": round(mfe * direction, 4) if direction < 0 else round(mfe, 4),
        }
        closed.append(record)
        with records_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(
            f"CLOSE {event['type']} {event['symbol']} dir={event['direction']} "
            f"ret={raw_return:.3f}% mfe={mfe:.3f}%",
            flush=True,
        )
    state["open_events"] = remaining
    return closed


def run_once(state: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    closed = close_expired(state, args, args.records)
    events = evaluate_once(state, args)
    seen = set()
    for event in events:
        key = (event["type"], event["symbol"], event["ts"])
        if key in seen:
            continue
        seen.add(key)
        try:
            event["entry_price"] = fetch_price(event["symbol"])
        except Exception:
            continue
        state.setdefault("open_events", []).append(event)
        print(
            f"OPEN {event['type']} {event['symbol']} dir={event['direction']} "
            f"strength={event['strength']} z={event['z']}",
            flush=True,
        )
    args.state.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "closed": len(closed),
                "opened": len(events),
                "open_positions": len(state.get("open_events", [])),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return state


def main() -> None:
    args = parse_args()
    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.records.parent.mkdir(parents=True, exist_ok=True)
    state: dict[str, Any] = {}
    if args.state.exists():
        state = json.loads(args.state.read_text(encoding="utf-8"))
    if args.once:
        run_once(state, args)
        return
    while True:
        run_once(state, args)
        time.sleep(args.loop_interval)


if __name__ == "__main__":
    main()
