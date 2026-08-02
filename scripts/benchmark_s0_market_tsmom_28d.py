from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = (
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2020_2023",
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2024_2025",
    ROOT / "data" / "research" / "binance_um_point_in_time_1h",
)
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_market_tsmom_28d"
LOOKBACK_DAYS = 28
HOLD_DAYS = 5
HISTORY_DAYS = 180
TOP_LIQUID_SYMBOLS = 20
BASE_ONE_WAY_COST = 0.0006
STRESS_ONE_WAY_COST = 0.0012


def daily_symbol(path: Path) -> pd.DataFrame:
    hourly = pd.read_parquet(
        path, columns=["symbol", "open_time", "open", "close", "quote_volume"]
    ).sort_values("open_time")
    hourly["day"] = pd.to_datetime(hourly.open_time, unit="ms", utc=True).dt.floor(
        "D"
    )
    return (
        hourly.groupby(["symbol", "day"], as_index=False)
        .agg(open=("open", "first"), close=("close", "last"), quote_volume=("quote_volume", "sum"))
    )


def build_daily_panel(data_dirs: tuple[Path, ...]) -> pd.DataFrame:
    parts = [
        daily_symbol(path)
        for root in data_dirs
        for path in sorted((root / "parquet").glob("*.parquet"))
    ]
    if not parts:
        raise ValueError("No Binance point-in-time hourly archives found")
    panel = (
        pd.concat(parts, ignore_index=True)
        .sort_values(["symbol", "day"])
        .drop_duplicates(["symbol", "day"], keep="last")
        .reset_index(drop=True)
    )
    panel["return"] = panel.groupby("symbol").close.pct_change(fill_method=None)
    panel["median_volume_30d"] = panel.groupby("symbol").quote_volume.transform(
        lambda values: values.rolling(30, min_periods=20).median()
    )
    return panel


def market_state(panel: pd.DataFrame) -> pd.DataFrame:
    eligible = panel.dropna(subset=["return", "median_volume_30d"]).copy()
    eligible["liquidity_rank"] = eligible.groupby("day").median_volume_30d.rank(
        method="first", ascending=False
    )
    liquid = eligible.loc[eligible.liquidity_rank.le(TOP_LIQUID_SYMBOLS)].copy()
    daily = (
        liquid.groupby("day", as_index=False)
        .agg(market_return=("return", "mean"), universe_size=("symbol", "nunique"))
        .sort_values("day")
        .reset_index(drop=True)
    )
    daily["market_index"] = (1.0 + daily.market_return).cumprod()
    daily["momentum_28d"] = daily.market_index.pct_change(LOOKBACK_DAYS)
    daily["historical_top_third"] = (
        daily.momentum_28d.expanding(min_periods=HISTORY_DAYS)
        .quantile(2.0 / 3.0)
        .shift(1)
    )
    daily["signal"] = (
        daily.universe_size.ge(TOP_LIQUID_SYMBOLS)
        & daily.momentum_28d.gt(daily.historical_top_third)
    )
    return daily


def symbol_proxy_trades(
    panel: pd.DataFrame, state: pd.DataFrame, symbol: str
) -> pd.DataFrame:
    proxy = (
        panel.loc[panel.symbol.eq(symbol), ["day", "open"]]
        .drop_duplicates("day", keep="last")
        .sort_values("day")
        .reset_index(drop=True)
    )
    frame = state.merge(proxy, on="day", how="inner").sort_values("day").reset_index(
        drop=True
    )
    frame["entry_day"] = frame.day.shift(-1)
    frame["exit_day"] = frame.day.shift(-1 - HOLD_DAYS)
    frame["gross_return"] = frame.open.shift(-1 - HOLD_DAYS) / frame.open.shift(-1) - 1.0
    candidates = frame.loc[
        frame.signal & frame.gross_return.notna(),
        ["day", "entry_day", "exit_day", "gross_return", "momentum_28d", "historical_top_third"],
    ].copy()
    selected: list[int] = []
    available_day = pd.Timestamp.min.tz_localize("UTC")
    for row in candidates.itertuples():
        if row.entry_day >= available_day:
            selected.append(int(row.Index))
            available_day = row.exit_day
    return candidates.loc[selected].reset_index(drop=True)


def btc_proxy_trades(panel: pd.DataFrame, state: pd.DataFrame) -> pd.DataFrame:
    return symbol_proxy_trades(panel, state, "BTCUSDT")


def market_basket_trades(panel: pd.DataFrame, state: pd.DataFrame) -> pd.DataFrame:
    liquid = panel.dropna(subset=["median_volume_30d"]).copy()
    liquid["liquidity_rank"] = liquid.groupby("day").median_volume_30d.rank(
        method="first", ascending=False
    )
    liquid = liquid.loc[
        liquid.liquidity_rank.le(TOP_LIQUID_SYMBOLS),
        ["day", "symbol", "open", "liquidity_rank"],
    ]
    open_lookup = panel.set_index(["day", "symbol"]).open
    signal_days = state.loc[state.signal, "day"].sort_values()
    selected: list[dict[str, Any]] = []
    available_day = pd.Timestamp.min.tz_localize("UTC")
    all_days = pd.Index(state.day.sort_values().unique())
    day_positions = {day: position for position, day in enumerate(all_days)}
    for signal_day in signal_days:
        position = day_positions.get(signal_day)
        if position is None or position + 1 + HOLD_DAYS >= len(all_days):
            continue
        entry_day = all_days[position + 1]
        exit_day = all_days[position + 1 + HOLD_DAYS]
        if entry_day < available_day:
            continue
        symbols = liquid.loc[liquid.day.eq(signal_day)].sort_values(
            "liquidity_rank"
        ).symbol.tolist()
        returns: list[float] = []
        for symbol in symbols:
            try:
                entry = float(open_lookup.loc[(entry_day, symbol)])
                exit_price = float(open_lookup.loc[(exit_day, symbol)])
            except KeyError:
                continue
            if entry > 0 and exit_price > 0:
                returns.append(exit_price / entry - 1.0)
        if len(returns) < int(TOP_LIQUID_SYMBOLS * 0.75):
            continue
        selected.append(
            {
                "day": signal_day,
                "entry_day": entry_day,
                "exit_day": exit_day,
                "gross_return": float(np.mean(returns)),
                "constituents": len(returns),
            }
        )
        available_day = exit_day
    return pd.DataFrame(selected)


def metrics(trades: pd.DataFrame, one_way_cost: float) -> dict[str, float | int]:
    if trades.empty:
        return {"trades": 0, "win_rate": 0.0, "profit_factor": 0.0, "net_return": 0.0, "max_drawdown": 0.0}
    net = trades.gross_return - 2.0 * float(one_way_cost)
    wins = float(net.clip(lower=0).sum())
    losses = float(-net.clip(upper=0).sum())
    equity = (1.0 + net).cumprod()
    drawdown = equity / equity.cummax() - 1.0
    return {
        "trades": int(len(trades)),
        "win_rate": round(float(net.gt(0).mean() * 100.0), 4),
        "profit_factor": round(wins / losses, 4) if losses else (999.0 if wins else 0.0),
        "net_return": round(float(equity.iloc[-1] - 1.0), 6),
        "max_drawdown": round(float(drawdown.min()), 6),
    }


def evaluate(trades: pd.DataFrame, one_way_cost: float) -> dict[str, Any]:
    years = pd.to_datetime(trades.entry_day, utc=True).dt.year
    return {
        "overall": metrics(trades, one_way_cost),
        "annual": {
            str(int(year)): metrics(group, one_way_cost)
            for year, group in trades.assign(year=years).groupby("year", sort=True)
        },
    }


def select_proxy_on_training_period(
    proxy_trades: dict[str, pd.DataFrame], one_way_cost: float
) -> str:
    scores: dict[str, tuple[float, float, str]] = {}
    for symbol, trades in proxy_trades.items():
        training = trades.loc[
            pd.to_datetime(trades.entry_day, utc=True).dt.year.le(2023)
        ]
        result = metrics(training, one_way_cost)
        scores[symbol] = (
            float(result["profit_factor"]),
            float(result["net_return"]),
            symbol,
        )
    return max(scores, key=scores.get)


def qualifies(stress: dict[str, Any]) -> bool:
    required = {str(year) for year in range(2021, 2027)}
    annual = stress["annual"]
    return bool(
        required.issubset(annual)
        and stress["overall"]["profit_factor"] > 1.10
        and stress["overall"]["max_drawdown"] > -0.60
        and all(
            annual[year]["net_return"] > 0 and annual[year]["profit_factor"] > 1.0
            for year in required
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit 28-day market TSMOM as a BTC S0 proxy.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    panel = build_daily_panel(DEFAULT_DATA)
    state = market_state(panel)
    proxy_trades = {
        symbol: symbol_proxy_trades(panel, state, symbol)
        for symbol in ("BTCUSDT", "ETHUSDT")
    }
    basket_trades = market_basket_trades(panel, state)
    selected_proxy = select_proxy_on_training_period(
        proxy_trades, STRESS_ONE_WAY_COST
    )
    trades = proxy_trades[selected_proxy]
    reports = {
        symbol: {
            "base": evaluate(symbol_trades, BASE_ONE_WAY_COST),
            "stress": evaluate(symbol_trades, STRESS_ONE_WAY_COST),
        }
        for symbol, symbol_trades in proxy_trades.items()
    }
    reports["TOP20_MARKET_BASKET"] = {
        "base": evaluate(basket_trades, BASE_ONE_WAY_COST),
        "stress": evaluate(basket_trades, STRESS_ONE_WAY_COST),
    }
    selected_reports = reports[selected_proxy]
    accepted = qualifies(selected_reports["stress"])
    result = {
        "experiment": "s0_market_tsmom_28d_proxy_and_market_basket",
        "source_method": "Han-Kang-Ryu market time-series momentum proxy",
        "rule": "market 28d return above shifted historical top-third; long selected major proxy or fixed top-20 basket five days; otherwise cash",
        "proxy_selection": "choose BTC or ETH by 2020-2023 stress PF, then freeze for 2024-2026",
        "selected_proxy": selected_proxy,
        "top_liquid_symbols": TOP_LIQUID_SYMBOLS,
        "history_days": HISTORY_DAYS,
        "base_one_way_cost": BASE_ONE_WAY_COST,
        "stress_one_way_cost": STRESS_ONE_WAY_COST,
        "reports": reports,
        "basket_accepted_as_research_thesis": qualifies(
            reports["TOP20_MARKET_BASKET"]["stress"]
        ),
        "accepted": accepted,
        "decision": "eligible_for_hourly_path_validation" if accepted else "rejected_not_stable_positive_expectancy",
    }
    args.output.mkdir(parents=True, exist_ok=True)
    state.to_parquet(args.output / "market_state.parquet", index=False)
    for symbol, symbol_trades in proxy_trades.items():
        symbol_trades.to_parquet(
            args.output / f"{symbol.lower()}_proxy_trades.parquet", index=False
        )
    basket_trades.to_parquet(
        args.output / "top20_market_basket_trades.parquet", index=False
    )
    (args.output / "report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
