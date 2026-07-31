from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import benchmark_s0_spot_perp_basis as data_source


BASE_COST = 0.0012
STRESS_COST = 0.0024
MIN_DAILY_SPOT_QUOTE_VOLUME = 20_000_000.0


@dataclass(frozen=True)
class Profile:
    name: str
    direction: str
    stop_pct: float


PROFILES = tuple(
    Profile(f"{direction}_stop_{stop_pct:.0%}", direction, stop_pct)
    for direction in ("long_high", "short_low", "strongest_side")
    for stop_pct in (0.10, 0.20, 0.30)
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit the published daily cryptocurrency futures basis factor."
    )
    parser.add_argument(
        "--roots",
        type=Path,
        nargs="+",
        default=[
            Path("data/research/binance_um_point_in_time_1h_2020_2023"),
            Path("data/research/binance_um_point_in_time_1h_2024_2025"),
            Path("data/research/binance_um_point_in_time_1h"),
        ],
    )
    parser.add_argument(
        "--basis-cache",
        type=Path,
        default=Path("data/research/s0_spot_perp_basis/cache"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/research/s0_cross_sectional_basis"),
    )
    return parser.parse_args()


def daily_candidates(markets: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for symbol, market in markets.items():
        scoped = market.copy()
        scoped["time"] = pd.to_datetime(scoped.open_time, unit="ms", utc=True)
        scoped = scoped.loc[scoped.time.dt.hour.eq(23)].copy()
        scoped["day"] = scoped.time.dt.floor("D")
        scoped["basis_factor"] = scoped.close_spot / scoped.close_perp - 1.0
        scoped["eligible"] = scoped.spot_liquidity_24h.ge(
            MIN_DAILY_SPOT_QUOTE_VOLUME
        )
        rows.append(
            scoped[
                ["day", "open_time", "basis_factor", "eligible"]
            ].assign(symbol=symbol)
        )
    panel = pd.concat(rows, ignore_index=True)
    panel = panel.loc[panel.eligible].copy()
    panel["rank"] = panel.groupby("day").basis_factor.rank(pct=True)
    panel["median_basis"] = panel.groupby("day").basis_factor.transform("median")
    panel["distance"] = (panel.basis_factor - panel.median_basis).abs()
    return panel


def select_candidates(panel: pd.DataFrame, direction: str) -> pd.DataFrame:
    if direction == "long_high":
        selected = panel.sort_values(
            ["day", "basis_factor"], ascending=[True, False]
        ).drop_duplicates("day")
        selected = selected.assign(side="long")
    elif direction == "short_low":
        selected = panel.sort_values(
            ["day", "basis_factor"], ascending=[True, True]
        ).drop_duplicates("day")
        selected = selected.assign(side="short")
    elif direction == "strongest_side":
        selected = panel.sort_values(
            ["day", "distance"], ascending=[True, False]
        ).drop_duplicates("day")
        selected = selected.assign(
            side=np.where(selected.basis_factor.ge(selected.median_basis), "long", "short")
        )
    else:
        raise ValueError(f"Unknown direction: {direction}")
    return selected.sort_values("day").reset_index(drop=True)


def execute_daily_trade(
    market: pd.DataFrame,
    funding: pd.DataFrame,
    signal_time: int,
    side: str,
    stop_pct: float,
) -> dict[str, object] | None:
    entry_time = signal_time + 3_600_000
    entry_rows = market.index[market.open_time.eq(entry_time)]
    exit_rows = market.index[market.open_time.eq(entry_time + 24 * 3_600_000)]
    if len(entry_rows) != 1 or len(exit_rows) != 1:
        return None
    entry_index = int(entry_rows[0])
    time_exit_index = int(exit_rows[0])
    entry_price = float(market.loc[entry_index, "open_perp"])
    stop_price = entry_price * (1.0 - stop_pct if side == "long" else 1.0 + stop_pct)
    exit_index = time_exit_index
    exit_price = float(market.loc[time_exit_index, "open_perp"])
    exit_reason = "time"
    for index in range(entry_index, time_exit_index):
        row = market.loc[index]
        expected_time = entry_time + (index - entry_index) * 3_600_000
        if int(row.open_time) != expected_time:
            return None
        hit = row.low_perp <= stop_price if side == "long" else row.high_perp >= stop_price
        if hit:
            exit_index = index
            if side == "long":
                exit_price = min(float(row.open_perp), stop_price)
            else:
                exit_price = max(float(row.open_perp), stop_price)
            exit_reason = "stop"
            break
    exit_time = int(market.loc[exit_index, "open_time"])
    price_return = (
        exit_price / entry_price - 1.0
        if side == "long"
        else 1.0 - exit_price / entry_price
    )
    settled_funding = data_source.funding_pnl(funding, entry_time, exit_time)
    funding_return = -settled_funding if side == "long" else settled_funding
    return {
        "entry_time": entry_time,
        "exit_time": exit_time,
        "side": side,
        "price_return": float(price_return),
        "funding_return": float(funding_return),
        "gross_return": float(price_return + funding_return),
        "exit_reason": exit_reason,
    }


def simulate(
    panel: pd.DataFrame,
    markets: dict[str, pd.DataFrame],
    fundings: dict[str, pd.DataFrame],
    profile: Profile,
) -> pd.DataFrame:
    rows = []
    for candidate in select_candidates(panel, profile.direction).itertuples(index=False):
        result = execute_daily_trade(
            markets[candidate.symbol],
            fundings[candidate.symbol],
            int(candidate.open_time),
            str(candidate.side),
            profile.stop_pct,
        )
        if result is not None:
            rows.append(
                {
                    "symbol": candidate.symbol,
                    "signal_time": int(candidate.open_time),
                    "basis_factor": float(candidate.basis_factor),
                    "rank": float(candidate.rank),
                    **result,
                }
            )
    return pd.DataFrame(rows)


def metrics(values: pd.Series) -> dict[str, float | int]:
    values = values.dropna().astype(float)
    gains = float(values.loc[values.gt(0)].sum())
    losses = float(-values.loc[values.lt(0)].sum())
    equity = values.add(1.0).cumprod()
    drawdown = equity.div(equity.cummax()).sub(1.0)
    top_three = values.nlargest(3).index
    without_top = values.drop(top_three)
    without_gains = float(without_top.loc[without_top.gt(0)].sum())
    without_losses = float(-without_top.loc[without_top.lt(0)].sum())
    return {
        "trades": int(len(values)),
        "win_rate": float(values.gt(0).mean()) if len(values) else 0.0,
        "profit_factor": gains / losses if losses else (999.0 if gains else 0.0),
        "net_return_points": float(values.sum()),
        "mean_return": float(values.mean()) if len(values) else 0.0,
        "max_drawdown": float(drawdown.min()) if len(drawdown) else 0.0,
        "without_top3_profit_factor": (
            without_gains / without_losses
            if without_losses
            else (999.0 if without_gains else 0.0)
        ),
        "without_top3_net_return_points": float(without_top.sum()),
    }


def report_scope(trades: pd.DataFrame, start: str, end: str) -> dict[str, object]:
    start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    end_ms = int(pd.Timestamp(end, tz="UTC").timestamp() * 1000)
    scoped = trades.loc[trades.entry_time.ge(start_ms) & trades.entry_time.lt(end_ms)]
    return {
        "base": metrics(scoped.gross_return - BASE_COST),
        "stress": metrics(scoped.gross_return - STRESS_COST),
        "symbols": int(scoped.symbol.nunique()),
        "long_trades": int(scoped.side.eq("long").sum()),
        "short_trades": int(scoped.side.eq("short").sum()),
    }


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    markets: dict[str, pd.DataFrame] = {}
    fundings: dict[str, pd.DataFrame] = {}
    for symbol in data_source.SYMBOLS:
        market, funding = data_source.load_market(
            args.roots, args.basis_cache, symbol, False
        )
        if not market.empty:
            markets[symbol] = market
            fundings[symbol] = funding
    panel = daily_candidates(markets)
    simulations = {profile.name: simulate(panel, markets, fundings, profile) for profile in PROFILES}
    validation = {
        profile.name: report_scope(simulations[profile.name], "2022-01-01", "2023-01-01")
        for profile in PROFILES
    }
    qualified = [
        profile
        for profile in PROFILES
        if validation[profile.name]["stress"]["trades"] >= 250
        and validation[profile.name]["stress"]["profit_factor"] >= 1.10
        and validation[profile.name]["stress"]["without_top3_profit_factor"] >= 1.05
        and validation[profile.name]["stress"]["net_return_points"] > 0
    ]
    frozen = (
        max(
            qualified,
            key=lambda profile: (
                validation[profile.name]["stress"]["without_top3_profit_factor"],
                validation[profile.name]["stress"]["profit_factor"],
            ),
        )
        if qualified
        else None
    )
    annual = {}
    if frozen is not None:
        for year in range(2023, 2027):
            annual[str(year)] = report_scope(
                simulations[frozen.name], f"{year}-01-01", f"{year + 1}-01-01"
            )
    accepted = bool(
        frozen is not None
        and all(item["stress"]["trades"] >= 150 for item in annual.values())
        and all(item["stress"]["profit_factor"] >= 1.10 for item in annual.values())
        and all(item["stress"]["without_top3_profit_factor"] >= 1.05 for item in annual.values())
        and all(item["stress"]["net_return_points"] > 0 for item in annual.values())
    )
    report = {
        "experiment": "s0_cross_sectional_basis",
        "paper": "https://doi.org/10.1002/fut.22425",
        "method": "Daily cross-sectional spot-minus-perpetual basis factor, next-hour entry, one-day hold, actual funding, one S0 position, and exchange-executable stop path.",
        "symbols": list(markets),
        "costs": {"base": BASE_COST, "stress": STRESS_COST},
        "validation_2022": validation,
        "frozen_profile": asdict(frozen) if frozen else None,
        "annual_out_of_sample": annual,
        "accepted": accepted,
        "live_qualified": False,
        "decision": "forward_shadow_required" if accepted else "rejected_not_stable_positive_expectancy",
    }
    if frozen is not None:
        simulations[frozen.name].to_parquet(
            args.output / "frozen_trades.parquet", index=False
        )
    (args.output / "report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
