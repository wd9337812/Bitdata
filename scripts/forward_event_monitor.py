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

from scripts.evaluate_forward_event_records import TARGETS  # noqa: E402

DEFAULT_STATE = ROOT / "data" / "research" / "s0_forward_event_monitor" / "state.json"
DEFAULT_RECORDS = ROOT / "data" / "research" / "s0_forward_event_monitor" / "records.jsonl"
FALLBACK_SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT", "BNBUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT", "BCHUSDT",
    "XLMUSDT", "UNIUSDT", "AAVEUSDT", "FILUSDT", "NEARUSDT", "HBARUSDT",
    "ARBUSDT", "SUIUSDT", "OPUSDT", "INJUSDT", "WLDUSDT", "PEPEUSDT",
    "SHIBUSDT", "TRXUSDT", "ATOMUSDT", "ETCUSDT", "APTUSDT", "TIAUSDT",
    "1000PEPEUSDT", "WIFUSDT", "POPCATUSDT", "NEIROUSDT", "NOTUSDT",
    "MEWUSDT", "MOODENGUSDT", "TRUMPUSDT", "KAITOUSDT", "LABUSDT",
    "1000BONKUSDT", "ONDOUSDT",
)

POLYMARKET_SYMBOLS = {
    "bitcoin": "BTCUSDT",
    "btc": "BTCUSDT",
    "ethereum": "ETHUSDT",
    "eth": "ETHUSDT",
    "solana": "SOLUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
    "dogecoin": "DOGEUSDT",
    "doge": "DOGEUSDT",
    "bnb": "BNBUSDT",
}


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


def _polymarket_direction(title: str) -> int | None:
    lowered = title.lower()
    if any(token in lowered for token in ("above", "up", "increase", "higher", "rise")):
        return 1
    if any(token in lowered for token in ("below", "down", "decrease", "lower", "fall")):
        return -1
    return None


def _polymarket_symbol(title: str) -> str | None:
    lowered = title.lower()
    for token, symbol in POLYMARKET_SYMBOLS.items():
        if token in lowered:
            return symbol
    return None


def _market_probability(market: dict[str, Any]) -> float | None:
    raw = market.get("outcomePrices") or market.get("outcome_prices")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if isinstance(raw, list) and raw:
        try:
            return float(raw[0])
        except (TypeError, ValueError):
            return None
    for key in ("probability", "lastTradePrice", "last_trade_price"):
        try:
            value = float(market.get(key))
        except (TypeError, ValueError):
            continue
        if 0.0 <= value <= 1.0:
            return value
    return None


def evaluate_polymarket_binance(
    state: dict[str, Any], args: argparse.Namespace
) -> list[dict[str, Any]]:
    """Record only cross-market confirmations; Polymarket is never a direct order source."""
    try:
        raw_events = _get_json(
            "https://gamma-api.polymarket.com/events",
            {"active": "true", "closed": "false", "limit": "100"},
        )
    except Exception:
        return []
    rows = raw_events if isinstance(raw_events, list) else raw_events.get("data", [])
    cache = dict(state.get("polymarket_probability_cache") or {})
    next_cache = dict(cache)
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or row.get("question") or "")
        symbol = _polymarket_symbol(title)
        title_direction = _polymarket_direction(title)
        markets = row.get("markets") or [row]
        market = next((item for item in markets if isinstance(item, dict)), None)
        if not symbol or title_direction is None or not market:
            continue
        probability = _market_probability(market)
        if probability is None:
            continue
        market_id = str(market.get("id") or row.get("id") or title)
        previous = cache.get(market_id)
        next_cache[market_id] = probability
        if previous is None:
            continue
        probability_delta = probability - float(previous)
        if abs(probability_delta) < 0.08:
            continue
        # A falling probability for “will BTC rise?” is bearish; a falling
        # probability for “will BTC fall?” is bullish. The title alone cannot
        # provide an executable direction.
        direction = title_direction if probability_delta > 0 else -title_direction
        try:
            klines = fetch_klines(symbol, "5m", 24)
        except Exception:
            continue
        if len(klines) < 12:
            continue
        volumes = [float(item[5]) for item in klines[:-1]]
        last = klines[-1]
        entry = float(last[4])
        prior = float(klines[-4][4])
        move_pct = (entry / prior - 1.0) * 100.0 if prior > 0 else 0.0
        volume_z = z_score(volumes, float(last[5]))
        direction_ok = move_pct * direction > 0
        confirmation_count = int(direction_ok) + int(abs(volume_z) >= 1.5)
        if confirmation_count < 2:
            continue
        result.append(
            {
                "type": "polymarket_binance_confirmed",
                "event_id": f"poly:{market_id}:{int(time.time() // 300)}",
                "source_market_id": market_id,
                "symbol": symbol,
                "direction": direction,
                "title_direction": title_direction,
                "strength": round(abs(probability_delta) * 100.0, 3),
                "z": round(volume_z, 3),
                "probability": round(probability, 6),
                "probability_delta": round(probability_delta, 6),
                "market_title": title[:240],
                "volume_z": round(volume_z, 3),
                "move_pct": round(move_pct, 4),
                "binance_confirmation_count": confirmation_count,
                "entry_price": entry,
                "ts": int(time.time() * 1000),
                "horizon_hours": 6.0,
            }
        )
    # Gamma's active catalog is large and changes over time. Keep only the
    # current cycle's recognized crypto-event probabilities in the cache.
    state["polymarket_probability_cache"] = dict(list(next_cache.items())[-500:])
    return result


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


def simulate_open_tail(
    klines: list[list[Any]],
    first_idx: int,
    entry_price: float,
    stop_pct: float,
    target_pct: float,
    horizon_bars: int = 72,
) -> tuple[str, float, int] | None:
    """Adverse-first hourly paper trade of the new-listing open-tail candidate.

    Entry is the detected listing price; the first hourly bar at/after the
    listing time is the first bar considered. Returns (outcome, pnl_pct,
    exit_ms) or None when the path is empty.
    """
    if (
        entry_price <= 0
        or first_idx is None
        or first_idx < 0
        or first_idx >= len(klines)
    ):
        return None
    stop = entry_price * (1.0 - stop_pct / 100.0)
    target = entry_price * (1.0 + target_pct / 100.0)
    end = min(len(klines), first_idx + horizon_bars)
    for index in range(first_idx, end):
        high = float(klines[index][2])
        low = float(klines[index][3])
        if low <= stop:
            exit_price = stop
            outcome = "STOP"
        elif high >= target:
            exit_price = target
            outcome = "TARGET"
        else:
            continue
        return outcome, (exit_price / entry_price - 1.0) * 100.0, int(
            klines[index][0]
        )
    exit_price = float(klines[end - 1][4])
    return "TIME", (exit_price / entry_price - 1.0) * 100.0, int(
        klines[end - 1][0]
    )


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


def detect_new_listings(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Detect newly listed Binance USD-M symbols via exchangeInfo diff."""
    exchange = _get_json("https://fapi.binance.com/fapi/v1/exchangeInfo")
    current = {
        str(item["symbol"])
        for item in exchange.get("symbols", [])
        if str(item["symbol"]).endswith("USDT")
    }
    known = set(state.get("known_symbols", []))
    events: list[dict[str, Any]] = []
    if known:
        for symbol in sorted(current - known):
            try:
                price = fetch_price(symbol)
            except Exception:
                continue
            events.append(
                {
                    "type": "new_listing",
                    "symbol": symbol,
                    "direction": None,
                    "strength": 0.0,
                    "z": 0.0,
                    "ts": int(time.time() * 1000),
                    "entry_price": price,
                    "horizon_hours": 72.0,
                }
            )
    state["known_symbols"] = sorted(current)
    return events


def evaluate_once(
    state: dict[str, Any], args: argparse.Namespace, include_slow: bool = True
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    events.extend(evaluate_polymarket_binance(state, args))
    if not include_slow:
        return events
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
    events.extend(detect_new_listings(state))
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
        mfe = 0.0
        for item in klines:
            high = float(item[2])
            low = float(item[3])
            favorable = (
                max(
                    high / event["entry_price"] - 1.0,
                    event["entry_price"] / low - 1.0,
                )
                * 100.0
            )
            mfe = max(mfe, favorable)
        if direction:
            raw_return = (current / event["entry_price"] - 1.0) * direction * 100.0
            mfe_record = mfe * direction if direction < 0 else mfe
        else:
            raw_return = abs(current / event["entry_price"] - 1.0) * 100.0
            mfe_record = mfe
        first_hour_pct = None
        would_trade = False
        trade_pnl_pct = None
        open_tail_s15 = None
        open_tail_s20 = None
        if event.get("type") == "new_listing":
            first_idx = None
            for index, item in enumerate(klines):
                if int(item[0]) >= event["ts"]:
                    first_idx = index
                    break
            if first_idx is not None and first_idx + 1 < len(klines):
                open_price = float(klines[first_idx][1])
                close_price = float(klines[first_idx][4])
                if open_price > 0:
                    first_hour_pct = (close_price / open_price - 1.0) * 100.0
                    if abs(first_hour_pct) >= 5.0:
                        would_trade = True
                        direction_rule = 1 if first_hour_pct > 0 else -1
                        entry = close_price
                        stop = entry - direction_rule * entry * 0.15
                        target = entry + direction_rule * entry * 0.30
                        exit_price = None
                        exit_ms = int(klines[-1][0])
                        for item in klines[first_idx + 1:]:
                            high = float(item[2])
                            low = float(item[3])
                            if direction_rule > 0 and low <= stop:
                                exit_price, exit_ms = stop, int(item[0])
                                break
                            if direction_rule < 0 and high >= stop:
                                exit_price, exit_ms = stop, int(item[0])
                                break
                            if direction_rule > 0 and high >= target:
                                exit_price, exit_ms = target, int(item[0])
                                break
                            if direction_rule < 0 and low <= target:
                                exit_price, exit_ms = target, int(item[0])
                                break
                        if exit_price is None:
                            exit_price = float(klines[-1][4])
                        trade_pnl_pct = (
                            direction_rule * (exit_price / entry - 1.0) * 100.0
                        )
            open_tail_s15 = simulate_open_tail(
                klines, first_idx, event.get("entry_price", 0.0), 15.0, 50.0
            )
            open_tail_s20 = simulate_open_tail(
                klines, first_idx, event.get("entry_price", 0.0), 20.0, 50.0
            )
        record = {
            **event,
            "exit_ts": int(time.time() * 1000),
            "raw_return_pct": round(raw_return, 4),
            "mfe_pct": round(mfe_record, 4),
            "first_hour_return_pct": (
                round(first_hour_pct, 4) if first_hour_pct is not None else None
            ),
            "first_hour_rule_traded": would_trade,
            "first_hour_rule_pnl_pct": (
                round(trade_pnl_pct, 4) if trade_pnl_pct is not None else None
            ),
            "open_tail_s15_tp50_outcome": (
                open_tail_s15[0] if open_tail_s15 is not None else None
            ),
            "open_tail_s15_tp50_pnl_pct": (
                round(open_tail_s15[1], 4) if open_tail_s15 is not None else None
            ),
            "open_tail_s20_tp50_outcome": (
                open_tail_s20[0] if open_tail_s20 is not None else None
            ),
            "open_tail_s20_tp50_pnl_pct": (
                round(open_tail_s20[1], 4) if open_tail_s20 is not None else None
            ),
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


def check_milestones(records_path: Path) -> list[str]:
    """Return event types that reached their forward-sample target."""
    counts: dict[str, int] = {}
    if records_path.exists():
        with records_path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                event_type = str(record.get("type", "unknown"))
                counts[event_type] = counts.get(event_type, 0) + 1
    reached = [
        event_type
        for event_type, target in TARGETS.items()
        if counts.get(event_type, 0) >= target
    ]
    for event_type in reached:
        print(
            f"MILESTONE_REACHED type={event_type} closed={counts[event_type]}",
            flush=True,
        )
    return reached


def run_once(state: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    closed = close_expired(state, args, args.records)
    check_milestones(args.records)
    now_epoch = time.time()
    slow_interval = max(300.0, float(args.loop_interval))
    include_slow = now_epoch - float(state.get("last_slow_scan_epoch") or 0) >= slow_interval
    events = evaluate_once(state, args, include_slow=include_slow)
    if include_slow:
        state["last_slow_scan_epoch"] = now_epoch
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
    state["latest_events"] = events[-100:]
    state["last_completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state["last_completed_epoch"] = now_epoch
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
