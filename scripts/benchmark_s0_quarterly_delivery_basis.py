from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.download_binance_cm_quarterly_basis_1d import EXPIRIES, expiry_timestamp

DEFAULT_BASIS = ROOT / "data" / "research" / "binance_cm_quarterly_basis_1d"
DEFAULT_EXECUTION = ROOT / "data" / "research" / "binance_um_prefunding_5m"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_quarterly_delivery_basis"
UNDERLYINGS = ("BTC", "ETH", "BNB", "ADA", "LINK", "BCH", "XRP", "DOT", "LTC")
BASE_COST = 0.0012
STRESS_COST = 0.0024
MIN_PRIOR_24H_QUOTE_VOLUME = 20_000_000.0
MIN_CROSS_SECTION = 4
FIVE_MINUTES_MS = 5 * 60 * 1000
DAY_MS = 24 * 60 * 60 * 1000


@dataclass(frozen=True)
class Profile:
    name: str
    selection: str
    stop_pct: float | None


# Fixed before validation results are observed.
PROFILES = (
    Profile("paper_high_low", "high_low", None),
    Profile("paper_long_high", "long_high", None),
    Profile("paper_short_low", "short_low", None),
    Profile("s0_top_one", "top_one", 0.30),
    Profile("s0_strongest_stop10", "strongest", 0.10),
    Profile("s0_strongest_stop30", "strongest", 0.30),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit the peer-reviewed current-quarter futures basis factor on Binance."
    )
    parser.add_argument("--basis", type=Path, default=DEFAULT_BASIS)
    parser.add_argument("--execution", type=Path, default=DEFAULT_EXECUTION)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def read_tree(path: Path) -> pd.DataFrame:
    files = sorted(path.glob("*.parquet"))
    if not files:
        return pd.DataFrame()
    return (
        pd.concat([pd.read_parquet(file) for file in files], ignore_index=True)
        .drop_duplicates("open_time", keep="last")
        .sort_values("open_time")
        .reset_index(drop=True)
    )


def current_contract(base: str, signal_close_time: int, available: set[str]) -> str | None:
    signal_time = pd.to_datetime(signal_close_time, unit="ms", utc=True)
    for suffix in EXPIRIES:
        # Delivery happens at 08:00 UTC; the daily signal before it still uses the contract.
        delivery_time = expiry_timestamp(suffix) + pd.Timedelta(hours=8)
        symbol = f"{base}USD_{suffix}"
        if signal_time < delivery_time and symbol in available:
            return symbol
    return None


def build_basis_panel(root: Path) -> pd.DataFrame:
    delivery_root = root / "delivery"
    available = {path.name for path in delivery_root.iterdir() if path.is_dir()}
    delivery_cache = {symbol: read_tree(delivery_root / symbol) for symbol in available}
    rows: list[pd.DataFrame] = []
    for base in UNDERLYINGS:
        index = read_tree(root / "index" / f"{base}USD")
        if index.empty:
            continue
        index = index.loc[:, ["open_time", "close_time", "close"]].rename(
            columns={"close": "index_close"}
        )
        index["contract"] = index.close_time.map(
            lambda close_time: current_contract(base, int(close_time), available)
        )
        for contract, scoped in index.dropna(subset=["contract"]).groupby("contract"):
            delivery = delivery_cache[str(contract)].loc[:, ["open_time", "close"]].rename(
                columns={"close": "delivery_close"}
            )
            merged = scoped.merge(delivery, on="open_time", how="inner")
            merged["base"] = base
            merged["symbol"] = f"{base}USDT"
            merged["basis"] = merged.index_close / merged.delivery_close - 1.0
            rows.append(merged)
    panel = pd.concat(rows, ignore_index=True).sort_values(["close_time", "base"])
    panel = panel.loc[np.isfinite(panel.basis)].copy()
    counts = panel.groupby("close_time").base.transform("nunique")
    panel = panel.loc[counts.ge(MIN_CROSS_SECTION)].copy()
    panel["rank"] = panel.groupby("close_time").basis.rank(method="first", pct=True)
    panel["median_basis"] = panel.groupby("close_time").basis.transform("median")
    panel["distance"] = (panel.basis - panel.median_basis).abs()
    return panel.reset_index(drop=True)


def select(panel: pd.DataFrame, selection: str) -> pd.DataFrame:
    selected: list[pd.DataFrame] = []
    for _, day in panel.groupby("close_time", sort=True):
        ordered = day.sort_values("basis").reset_index(drop=True)
        bucket = max(1, len(ordered) // 3)
        low = ordered.head(bucket).assign(side="short", leg="low")
        high = ordered.tail(bucket).assign(side="long", leg="high")
        if selection == "high_low":
            chosen = pd.concat([high, low], ignore_index=True)
        elif selection == "long_high":
            chosen = high
        elif selection == "short_low":
            chosen = low
        elif selection == "top_one":
            chosen = ordered.tail(1).assign(side="long", leg="high")
        elif selection == "strongest":
            candidate = day.sort_values("distance").tail(1).copy()
            candidate["side"] = np.where(
                candidate.basis.ge(candidate.median_basis), "long", "short"
            )
            candidate["leg"] = "high" if candidate.iloc[0].side == "long" else "low"
            chosen = candidate
        else:
            raise ValueError(f"Unknown selection: {selection}")
        selected.append(chosen)
    return pd.concat(selected, ignore_index=True).sort_values(["close_time", "symbol"])


def load_execution(root: Path, symbol: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    market = read_tree(root / "klines" / symbol)
    funding_files = sorted((root / "fundingRate" / symbol).glob("*.parquet"))
    funding = (
        pd.concat([pd.read_parquet(file) for file in funding_files], ignore_index=True)
        .drop_duplicates("calc_time", keep="last")
        .sort_values("calc_time")
        .reset_index(drop=True)
        if funding_files
        else pd.DataFrame(columns=["calc_time", "last_funding_rate"])
    )
    return market, funding


def execute(
    market: pd.DataFrame,
    funding: pd.DataFrame,
    signal_close_time: int,
    side: str,
    stop_pct: float | None,
) -> dict[str, Any] | None:
    entry_time = signal_close_time + 1 + FIVE_MINUTES_MS
    exit_time = entry_time + DAY_MS
    times = market.open_time.to_numpy(dtype="int64")
    start = int(np.searchsorted(times, entry_time))
    end = int(np.searchsorted(times, exit_time))
    if start >= len(market) or end >= len(market):
        return None
    if int(times[start]) != entry_time or int(times[end]) != exit_time:
        return None
    path = market.iloc[start:end]
    if len(path) != DAY_MS // FIVE_MINUTES_MS:
        return None
    expected = entry_time + np.arange(len(path), dtype="int64") * FIVE_MINUTES_MS
    if not np.array_equal(path.open_time.to_numpy(dtype="int64"), expected):
        return None
    prior_start = int(np.searchsorted(times, entry_time - DAY_MS))
    prior = market.iloc[prior_start:start]
    if len(prior) != DAY_MS // FIVE_MINUTES_MS:
        return None
    if float(prior.quote_volume.sum()) < MIN_PRIOR_24H_QUOTE_VOLUME:
        return None
    entry_price = float(market.iloc[start].open)
    actual_exit_time = exit_time
    exit_price = float(market.iloc[end].open)
    exit_reason = "time"
    if stop_pct is not None:
        stop = entry_price * (1.0 - stop_pct if side == "long" else 1.0 + stop_pct)
        adverse = path.low.le(stop) if side == "long" else path.high.ge(stop)
        if bool(adverse.any()):
            hit = int(np.flatnonzero(adverse.to_numpy())[0])
            row = path.iloc[hit]
            actual_exit_time = int(row.open_time)
            exit_price = (
                min(float(row.open), stop) if side == "long" else max(float(row.open), stop)
            )
            exit_reason = "stop"
    sign = 1.0 if side == "long" else -1.0
    price_return = sign * (exit_price / entry_price - 1.0)
    settled = funding.loc[
        funding.calc_time.gt(entry_time) & funding.calc_time.le(actual_exit_time),
        "last_funding_rate",
    ].sum()
    funding_return = -float(settled) if side == "long" else float(settled)
    return {
        "entry_time": entry_time,
        "exit_time": actual_exit_time,
        "price_return": float(price_return),
        "funding_return": funding_return,
        "gross_return": float(price_return + funding_return),
        "exit_reason": exit_reason,
        "prior_quote_volume": float(prior.quote_volume.sum()),
    }


def simulate(
    panel: pd.DataFrame,
    profile: Profile,
    markets: dict[str, pd.DataFrame],
    fundings: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for candidate in select(panel, profile.selection).itertuples(index=False):
        if candidate.symbol not in markets:
            continue
        result = execute(
            markets[candidate.symbol],
            fundings[candidate.symbol],
            int(candidate.close_time),
            str(candidate.side),
            profile.stop_pct,
        )
        if result is not None:
            rows.append(
                {
                    "profile": profile.name,
                    "symbol": candidate.symbol,
                    "contract": candidate.contract,
                    "signal_time": int(candidate.close_time),
                    "side": candidate.side,
                    "leg": candidate.leg,
                    "basis": float(candidate.basis),
                    "rank": float(candidate.rank),
                    **result,
                }
            )
    return pd.DataFrame(rows)


def portfolio_returns(trades: pd.DataFrame, cost: float) -> pd.Series:
    if trades.empty:
        return pd.Series(dtype=float)
    net = trades.assign(net=trades.gross_return - cost)
    return net.groupby("entry_time").net.mean().sort_index()


def metrics(returns: pd.Series) -> dict[str, float | int]:
    values = returns.dropna().astype(float)
    gains = float(values.loc[values.gt(0)].sum())
    losses = float(-values.loc[values.lt(0)].sum())
    equity = values.add(1.0).cumprod()
    drawdown = equity.div(equity.cummax()).sub(1.0)
    without = values.drop(values.nlargest(min(3, len(values))).index)
    without_gains = float(without.loc[without.gt(0)].sum())
    without_losses = float(-without.loc[without.lt(0)].sum())
    return {
        "days": int(len(values)),
        "win_rate_pct": float(values.gt(0).mean() * 100.0) if len(values) else 0.0,
        "profit_factor": gains / losses if losses else (999.0 if gains else 0.0),
        "net_pct_points": float(values.sum() * 100.0),
        "compounded_return_pct": float((equity.iloc[-1] - 1.0) * 100.0) if len(equity) else 0.0,
        "max_drawdown_pct": float(drawdown.min() * 100.0) if len(drawdown) else 0.0,
        "without_top3_profit_factor": (
            without_gains / without_losses
            if without_losses
            else (999.0 if without_gains else 0.0)
        ),
        "without_top3_net_pct_points": float(without.sum() * 100.0),
    }


def scope(trades: pd.DataFrame, start: str, end: str) -> dict[str, Any]:
    start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    end_ms = int(pd.Timestamp(end, tz="UTC").timestamp() * 1000)
    scoped = trades.loc[trades.entry_time.ge(start_ms) & trades.entry_time.lt(end_ms)]
    return {
        "trades": int(len(scoped)),
        "symbols": int(scoped.symbol.nunique()) if len(scoped) else 0,
        "long_trades": int(scoped.side.eq("long").sum()) if len(scoped) else 0,
        "short_trades": int(scoped.side.eq("short").sum()) if len(scoped) else 0,
        "funding_pct_points": float(scoped.funding_return.sum() * 100.0) if len(scoped) else 0.0,
        "base": metrics(portfolio_returns(scoped, BASE_COST)),
        "stress": metrics(portfolio_returns(scoped, STRESS_COST)),
    }


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    panel = build_basis_panel(args.basis)
    symbols = sorted(panel.symbol.unique())
    loaded = {symbol: load_execution(args.execution, symbol) for symbol in symbols}
    markets = {symbol: pair[0] for symbol, pair in loaded.items() if not pair[0].empty}
    fundings = {symbol: pair[1] for symbol, pair in loaded.items() if not pair[0].empty}
    simulations = {
        profile.name: simulate(panel, profile, markets, fundings) for profile in PROFILES
    }
    windows = {
        "validation_2022_2023": ("2022-01-01", "2024-01-01"),
        "oos_2024": ("2024-01-01", "2025-01-01"),
        "oos_2025": ("2025-01-01", "2026-01-01"),
        "oos_2026_h1": ("2026-01-01", "2026-07-01"),
        "oos_combined": ("2024-01-01", "2026-07-01"),
    }
    results = {
        profile.name: {
            "profile": asdict(profile),
            "windows": {
                name: scope(simulations[profile.name], start, end)
                for name, (start, end) in windows.items()
            },
        }
        for profile in PROFILES
    }
    qualified = [
        profile
        for profile in PROFILES
        if results[profile.name]["windows"]["validation_2022_2023"]["stress"]["days"] >= 400
        and results[profile.name]["windows"]["validation_2022_2023"]["stress"]["profit_factor"] >= 1.10
        and results[profile.name]["windows"]["validation_2022_2023"]["stress"]["without_top3_profit_factor"] >= 1.05
    ]
    frozen = max(
        qualified,
        key=lambda profile: results[profile.name]["windows"]["validation_2022_2023"]["stress"]["profit_factor"],
        default=None,
    )
    live_qualified = False
    if frozen is not None:
        frozen_windows = results[frozen.name]["windows"]
        live_qualified = (
            all(
                frozen_windows[name]["stress"]["net_pct_points"] > 0
                for name in ("oos_2024", "oos_2025", "oos_2026_h1")
            )
            and frozen_windows["oos_combined"]["stress"]["profit_factor"] >= 1.15
            and frozen_windows["oos_combined"]["stress"]["without_top3_profit_factor"] >= 1.05
        )
    report = {
        "experiment": "s0_quarterly_delivery_basis",
        "paper_source": "Chi, Hao, and Jiang, An empirical investigation on risk factors in cryptocurrency futures",
        "method": (
            "Rank completed Binance COIN-M current-quarter delivery basis daily, then execute "
            "the corresponding USDT perpetual from T+5m for 24h with actual funding and 5m stop paths."
        ),
        "information_boundary": "Signal uses only the completed UTC daily index and delivery bars.",
        "costs": {"base_round_trip": BASE_COST, "stress_round_trip": STRESS_COST},
        "panel_rows": int(len(panel)),
        "panel_symbols": symbols,
        "profiles": results,
        "validation_qualified": [profile.name for profile in qualified],
        "frozen_profile": frozen.name if frozen else None,
        "live_qualified": live_qualified,
        "decision": "qualified_for_shadow_review" if live_qualified else "rejected_not_robust_oos",
    }
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for name, trades in simulations.items():
        trades.to_parquet(args.output / f"{name}.parquet", index=False)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
