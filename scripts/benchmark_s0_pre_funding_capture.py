from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    from scripts.benchmark_s0_daily_order_flow import metric
except ModuleNotFoundError:
    from benchmark_s0_daily_order_flow import metric


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data" / "research" / "binance_um_prefunding_5m"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_pre_funding_capture"
BASE_COST = 0.0012
STRESS_COST = 0.0024
MIN_LIQUIDITY_24H = 20_000_000.0
STOP = 0.02
TARGET = 0.02
EVALUATION_YEARS = (2022, 2023, 2024, 2025, 2026)


@dataclass(frozen=True)
class Candidate:
    name: str
    minimum_estimated_funding: float


CANDIDATES = (
    Candidate("estimated_funding_covers_base_cost", BASE_COST),
    Candidate("estimated_funding_covers_stress_cost", STRESS_COST),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit pre-settlement funding capture without using future funding rates."
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def load_symbol(data: Path, kind: str, symbol: str) -> pd.DataFrame:
    paths = sorted((data / kind / symbol).glob("*.parquet"))
    if not paths:
        return pd.DataFrame()
    return (
        pd.concat([pd.read_parquet(path) for path in paths], ignore_index=True)
        .sort_values("calc_time" if kind == "fundingRate" else "open_time")
        .drop_duplicates("calc_time" if kind == "fundingRate" else "open_time")
        .reset_index(drop=True)
    )


def estimated_funding(
    premium: pd.DataFrame,
    funding_time_ms: int,
    interval_hours: float,
    entry_lead_ms: int = 300_000,
) -> float:
    entry_ms = funding_time_ms - entry_lead_ms
    interval_start = funding_time_ms - int(interval_hours * 3_600_000)
    visible = premium.loc[
        premium.open_time.ge(interval_start) & premium.close_time.lt(entry_ms)
    ]
    if visible.empty:
        return float("nan")
    values = visible.close.to_numpy(dtype=float)
    average_premium = float(np.average(values, weights=np.arange(1, len(values) + 1)))
    interest = 0.0001 * interval_hours / 8.0
    return average_premium + float(np.clip(interest - average_premium, -0.0005, 0.0005))


def premium_estimator_arrays(premium: pd.DataFrame) -> tuple[np.ndarray, ...]:
    close_times = premium.close_time.to_numpy(dtype=np.int64)
    values = premium.close.to_numpy(dtype=float)
    indices = np.arange(1, len(values) + 1, dtype=float)
    prefix_values = np.concatenate(([0.0], np.cumsum(values)))
    prefix_weighted = np.concatenate(([0.0], np.cumsum(indices * values)))
    return close_times, prefix_values, prefix_weighted


def estimated_funding_fast(
    arrays: tuple[np.ndarray, ...],
    funding_time_ms: int,
    interval_hours: float,
    entry_lead_ms: int = 300_000,
) -> float:
    close_times, prefix_values, prefix_weighted = arrays
    interval_start = funding_time_ms - int(interval_hours * 3_600_000)
    entry_ms = funding_time_ms - entry_lead_ms
    left = int(np.searchsorted(close_times, interval_start, side="left"))
    right = int(np.searchsorted(close_times, entry_ms, side="left"))
    count = right - left
    if count <= 0:
        return float("nan")
    value_sum = prefix_values[right] - prefix_values[left]
    global_weighted_sum = prefix_weighted[right] - prefix_weighted[left]
    local_weighted_sum = global_weighted_sum - left * value_sum
    average_premium = float(local_weighted_sum / (count * (count + 1) / 2.0))
    interest = 0.0001 * interval_hours / 8.0
    return average_premium + float(np.clip(interest - average_premium, -0.0005, 0.0005))


def event_rows(data: Path, symbol: str) -> pd.DataFrame:
    premium = load_symbol(data, "premiumIndexKlines", symbol)
    funding = load_symbol(data, "fundingRate", symbol)
    prices = load_symbol(data, "klines", symbol)
    if premium.empty or funding.empty or prices.empty:
        return pd.DataFrame()
    prices = prices.copy()
    prices["prior_liquidity_24h"] = prices.quote_volume.shift(1).rolling(288).sum()
    indexed = prices.set_index("open_time", drop=False)
    estimator_arrays = premium_estimator_arrays(premium)
    rows: list[dict[str, Any]] = []
    for settlement in funding.itertuples(index=False):
        funding_time_ms = int(settlement.calc_time // 300_000 * 300_000)
        entry_ms = funding_time_ms - 300_000
        exit_ms = funding_time_ms + 300_000
        if entry_ms not in indexed.index or funding_time_ms not in indexed.index:
            continue
        estimate = estimated_funding_fast(
            estimator_arrays, funding_time_ms, float(settlement.funding_interval_hours)
        )
        entry_row = indexed.loc[entry_ms]
        rows.append(
            {
                "symbol": symbol,
                "funding_time_ms": funding_time_ms,
                "entry_ms": entry_ms,
                "exit_ms": exit_ms,
                "estimated_funding": estimate,
                "actual_funding": float(settlement.last_funding_rate),
                "liquidity_24h": float(entry_row.prior_liquidity_24h),
            }
        )
    return pd.DataFrame(rows)


def path_result(
    prices: pd.DataFrame,
    entry_ms: int,
    funding_time_ms: int,
    direction: float,
    actual_funding: float,
) -> dict[str, Any] | None:
    indexed = prices.set_index("open_time", drop=False)
    if entry_ms not in indexed.index or funding_time_ms not in indexed.index:
        return None
    entry = float(indexed.loc[entry_ms, "open"])
    stop_price = entry * (1.0 - direction * STOP)
    target_price = entry * (1.0 + direction * TARGET)
    for timestamp, receives_funding in ((entry_ms, False), (funding_time_ms, True)):
        row = indexed.loc[timestamp]
        if direction > 0:
            gap_stop = float(row.open) <= stop_price
            hit_stop = float(row.low) <= stop_price
            gap_target = float(row.open) >= target_price
            hit_target = float(row.high) >= target_price
        else:
            gap_stop = float(row.open) >= stop_price
            hit_stop = float(row.high) >= stop_price
            gap_target = float(row.open) <= target_price
            hit_target = float(row.low) <= target_price
        funding_pnl = -direction * actual_funding if receives_funding else 0.0
        if gap_stop:
            gross = direction * (float(row.open) / entry - 1.0)
            return {"gross_return": gross + funding_pnl, "funding_pnl": funding_pnl, "exit_reason": "stop_gap"}
        if hit_stop:
            return {"gross_return": -STOP + funding_pnl, "funding_pnl": funding_pnl, "exit_reason": "stop"}
        if gap_target:
            gross = direction * (float(row.open) / entry - 1.0)
            return {"gross_return": gross + funding_pnl, "funding_pnl": funding_pnl, "exit_reason": "target_gap"}
        if hit_target:
            return {"gross_return": TARGET + funding_pnl, "funding_pnl": funding_pnl, "exit_reason": "target"}
    exit_ms = funding_time_ms + 300_000
    if exit_ms not in indexed.index:
        return None
    price_return = direction * (float(indexed.loc[exit_ms, "open"]) / entry - 1.0)
    funding_pnl = -direction * actual_funding
    return {
        "gross_return": price_return + funding_pnl,
        "funding_pnl": funding_pnl,
        "exit_reason": "time",
    }


def collect_events(data: Path) -> pd.DataFrame:
    root = data / "fundingRate"
    parts = [event_rows(data, path.name) for path in sorted(root.iterdir()) if path.is_dir()]
    parts = [part for part in parts if not part.empty]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def simulate(data: Path, events: pd.DataFrame, candidate: Candidate) -> pd.DataFrame:
    eligible = events.loc[
        events.estimated_funding.abs().ge(candidate.minimum_estimated_funding)
        & events.liquidity_24h.ge(MIN_LIQUIDITY_24H)
    ].copy()
    eligible["estimated_receipt"] = eligible.estimated_funding.abs()
    eligible = (
        eligible.sort_values(
            ["funding_time_ms", "estimated_receipt", "liquidity_24h"],
            ascending=[True, False, False],
        )
        .groupby("funding_time_ms", sort=False)
        .head(1)
        .sort_values("funding_time_ms")
    )
    price_cache: dict[str, pd.DataFrame] = {}
    rows = []
    for event in eligible.itertuples(index=False):
        if event.symbol not in price_cache:
            price_cache[event.symbol] = load_symbol(data, "klines", event.symbol)
        direction = -float(np.sign(event.estimated_funding))
        result = path_result(
            price_cache[event.symbol],
            int(event.entry_ms),
            int(event.funding_time_ms),
            direction,
            float(event.actual_funding),
        )
        if result is None:
            continue
        rows.append(
            {
                "symbol": event.symbol,
                "funding_time": pd.to_datetime(event.funding_time_ms, unit="ms", utc=True),
                "direction": "LONG" if direction > 0 else "SHORT",
                "estimated_funding": float(event.estimated_funding),
                "actual_funding": float(event.actual_funding),
                **result,
            }
        )
    return pd.DataFrame(rows)


def summarize(trades: pd.DataFrame) -> dict[str, Any]:
    annual: dict[str, Any] = {}
    years = trades.funding_time.dt.year if not trades.empty else pd.Series(dtype=int)
    for year in EVALUATION_YEARS:
        scoped = trades.loc[years.eq(year)]
        annual[str(year)] = {
            "base": metric(scoped.gross_return - BASE_COST),
            "stress": metric(scoped.gross_return - STRESS_COST),
            "funding_receipt_pct_points": round(float(scoped.funding_pnl.sum() * 100), 6),
        }
    return annual


def diagnostics(trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty:
        return {
            "trades": 0,
            "funding_receipt_pct_points": 0.0,
            "price_path_pct_points": 0.0,
            "estimate_sign_accuracy_pct": 0.0,
            "estimate_mae_bps": 0.0,
            "stress_without_top3": metric(pd.Series(dtype=float)),
        }
    estimate_sign = np.sign(trades.estimated_funding.to_numpy(dtype=float))
    actual_sign = np.sign(trades.actual_funding.to_numpy(dtype=float))
    stress = trades.gross_return - STRESS_COST
    without_top3 = stress.drop(stress.nlargest(min(3, len(stress))).index)
    price_path = trades.gross_return - trades.funding_pnl
    return {
        "trades": int(len(trades)),
        "funding_receipt_pct_points": round(float(trades.funding_pnl.sum() * 100), 6),
        "price_path_pct_points": round(float(price_path.sum() * 100), 6),
        "estimate_sign_accuracy_pct": round(float((estimate_sign == actual_sign).mean() * 100), 6),
        "estimate_mae_bps": round(
            float((trades.estimated_funding - trades.actual_funding).abs().mean() * 10_000),
            6,
        ),
        "stress_without_top3": metric(without_top3),
        "direction_counts": {
            str(direction): int(count)
            for direction, count in trades.direction.value_counts().items()
        },
    }


def run(data: Path) -> tuple[dict[str, Any], dict[str, pd.DataFrame]]:
    events = collect_events(data)
    reports: dict[str, Any] = {}
    trades_by_candidate: dict[str, pd.DataFrame] = {}
    for candidate in CANDIDATES:
        trades = simulate(data, events, candidate)
        trades_by_candidate[candidate.name] = trades
        annual = summarize(trades)
        passes = all(
            annual[str(year)]["stress"]["trades"] >= 10
            and annual[str(year)]["stress"]["profit_factor"] > 1.0
            and annual[str(year)]["stress"]["net_pct_points"] > 0.0
            for year in EVALUATION_YEARS
        )
        reports[candidate.name] = {
            "candidate": asdict(candidate),
            "annual": annual,
            "diagnostics": diagnostics(trades),
            "passes": passes,
        }
    accepted = [name for name, item in reports.items() if item["passes"]]
    report = {
        "experiment": "s0_pre_funding_capture",
        "method": "Use only premium-index bars completed five minutes before settlement to estimate funding; enter opposite the estimated funding sign, hold through settlement, and exit five minutes later with 2% stop/target and one cross-sectional S0 position.",
        "candidate_events": int(len(events)),
        "costs": {"base": BASE_COST, "stress": STRESS_COST},
        "candidates": reports,
        "accepted": accepted,
        "decision": "requires_fresh_forward_validation" if accepted else "rejected_not_positive_expectancy",
        "live_qualified": False,
    }
    return report, trades_by_candidate


def main() -> None:
    args = parse_args()
    report, trades_by_candidate = run(args.data)
    args.output.mkdir(parents=True, exist_ok=True)
    for name, trades in trades_by_candidate.items():
        trades.to_parquet(args.output / f"{name}.parquet", index=False)
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
