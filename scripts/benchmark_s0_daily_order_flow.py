from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DATA_DIRS = (
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2020_2023" / "parquet",
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2024_2025" / "parquet",
    ROOT / "data" / "research" / "binance_um_point_in_time_1h" / "parquet",
)
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_daily_order_flow"
BASE_COST = 0.0012
STRESS_COST = 0.0024
MIN_DAILY_QUOTE_VOLUME = 20_000_000.0
MIN_AGE_DAYS = 60
EVALUATION_YEARS = (2023, 2024, 2025, 2026)
FEATURES = (
    "order_flow_1",
    "order_flow_3",
    "order_flow_7",
    "order_flow_surprise",
    "return_1",
    "return_3",
    "return_7",
    "return_14",
    "volatility_7",
    "range_ratio",
    "volume_ratio_7",
    "cs_order_flow_1",
    "cs_order_flow_3",
    "cs_order_flow_7",
    "cs_return_1",
    "cs_return_7",
    "cs_quote_volume",
    "market_order_flow",
    "market_breadth",
    "btc_order_flow_1",
    "btc_return_1",
    "btc_return_7",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Point-in-time daily cross-sectional Binance order-flow proxy."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--panel", type=Path)
    return parser.parse_args()


def load_hourly_symbol(symbol: str, roots: tuple[Path, ...] = DATA_DIRS) -> pd.DataFrame:
    columns = (
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "quote_volume",
        "taker_buy_quote_volume",
    )
    parts = []
    for root in roots:
        path = root / f"{symbol}.parquet"
        if path.exists():
            parts.append(pd.read_parquet(path, columns=list(columns)))
    if not parts:
        return pd.DataFrame(columns=columns)
    return (
        pd.concat(parts, ignore_index=True)
        .sort_values("open_time")
        .drop_duplicates("open_time", keep="last")
        .reset_index(drop=True)
    )


def daily_from_hourly(hourly: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if hourly.empty:
        return pd.DataFrame()
    frame = hourly.copy()
    frame["date"] = pd.to_datetime(frame.open_time, unit="ms", utc=True).dt.floor("D")
    daily = (
        frame.groupby("date", sort=True)
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            quote_volume=("quote_volume", "sum"),
            taker_buy_quote_volume=("taker_buy_quote_volume", "sum"),
            hourly_bars=("open_time", "size"),
        )
        .reset_index()
    )
    daily = daily.loc[daily.hourly_bars.eq(24)].copy()
    daily["symbol"] = symbol
    return daily


def build_daily_panel(roots: tuple[Path, ...] = DATA_DIRS) -> pd.DataFrame:
    symbols = sorted({path.stem for root in roots for path in root.glob("*.parquet")})
    parts = []
    for symbol in symbols:
        daily = daily_from_hourly(load_hourly_symbol(symbol, roots), symbol)
        if not daily.empty:
            parts.append(daily)
    if not parts:
        raise FileNotFoundError("No hourly Binance parquet files found")
    return pd.concat(parts, ignore_index=True)


def engineer_features(panel: pd.DataFrame) -> pd.DataFrame:
    result = panel.sort_values(["symbol", "date"]).copy()
    result["sell_quote_volume"] = (
        result.quote_volume - result.taker_buy_quote_volume
    ).clip(lower=1.0)
    result["order_flow_1"] = np.log(
        (result.taker_buy_quote_volume.clip(lower=1.0)) / result.sell_quote_volume
    )
    grouped = result.groupby("symbol", sort=False)
    result["age_days"] = grouped.cumcount()
    for period in (3, 7):
        result[f"order_flow_{period}"] = (
            grouped.order_flow_1.rolling(period).mean().reset_index(level=0, drop=True)
        )
    prior_flow = grouped.order_flow_1.shift(1)
    result["order_flow_surprise"] = result.order_flow_1 - (
        prior_flow.groupby(result.symbol)
        .rolling(7)
        .mean()
        .reset_index(level=0, drop=True)
    )
    for period in (1, 3, 7, 14):
        result[f"return_{period}"] = grouped.close.pct_change(period, fill_method=None)
    result["volatility_7"] = (
        grouped.return_1.rolling(7).std().reset_index(level=0, drop=True)
    )
    result["range_ratio"] = (result.high - result.low) / result.close
    result["volume_ratio_7"] = result.quote_volume / (
        grouped.quote_volume.rolling(7).median().reset_index(level=0, drop=True)
    ).replace(0.0, np.nan)
    for column in ("order_flow_1", "order_flow_3", "order_flow_7", "return_1", "return_7", "quote_volume"):
        result[f"cs_{column}"] = result.groupby("date")[column].rank(pct=True)
    market = (
        result.groupby("date")
        .agg(
            market_order_flow=("order_flow_1", "mean"),
            market_breadth=("return_1", lambda values: float(values.gt(0.0).mean())),
        )
        .reset_index()
    )
    result = result.merge(market, on="date", how="left")
    btc = (
        result.loc[
            result.symbol.eq("BTCUSDT"),
            ["date", "order_flow_1", "return_1", "return_7"],
        ]
        .set_index("date")
        .add_prefix("btc_")
    )
    result = result.merge(btc, left_on="date", right_index=True, how="left")
    result = result.sort_values(["symbol", "date"]).reset_index(drop=True)
    grouped = result.groupby("symbol", sort=False)
    result["future_return"] = grouped.open.shift(-2) / grouped.open.shift(-1) - 1.0
    future_date = grouped.date.shift(-2)
    result.loc[
        future_date.sub(result.date).ne(pd.Timedelta(days=2)), "future_return"
    ] = np.nan
    result["entry_time"] = result.date + pd.Timedelta(days=1)
    result["exit_time"] = result.date + pd.Timedelta(days=2)
    eligible = (
        result.age_days.ge(MIN_AGE_DAYS)
        & result.quote_volume.ge(MIN_DAILY_QUOTE_VOLUME)
        & result.loc[:, FEATURES].notna().all(axis=1)
        & result.future_return.notna()
    )
    return result.loc[eligible].reset_index(drop=True)


def estimator() -> lgb.LGBMRegressor:
    return lgb.LGBMRegressor(
        objective="regression_l1",
        n_estimators=250,
        learning_rate=0.03,
        num_leaves=15,
        max_depth=5,
        min_child_samples=200,
        subsample=0.8,
        colsample_bytree=0.8,
        reg_lambda=2.0,
        random_state=42,
        verbosity=-1,
        n_jobs=4,
    )


def select_one_per_day(scored: pd.DataFrame) -> pd.DataFrame:
    candidates = scored.loc[scored.prediction.abs().ge(STRESS_COST)].copy()
    candidates["side"] = np.where(candidates.prediction.gt(0.0), 1, -1)
    candidates["expected_net"] = candidates.prediction.abs() - STRESS_COST
    return (
        candidates.sort_values(
            ["date", "expected_net", "quote_volume"],
            ascending=[True, False, False],
        )
        .drop_duplicates("date")
        .sort_values("date")
        .reset_index(drop=True)
    )


def walk_forward(features: pd.DataFrame) -> pd.DataFrame:
    selected = []
    for year in EVALUATION_YEARS:
        start = pd.Timestamp(f"{year}-01-01", tz="UTC")
        train = features.loc[
            features.exit_time.le(start) & features.date.ge("2020-03-01")
        ]
        current = features.loc[features.entry_time.dt.year.eq(year)].copy()
        model = estimator()
        model.fit(train.loc[:, FEATURES], train.future_return)
        current["prediction"] = model.predict(current.loc[:, FEATURES])
        chosen = select_one_per_day(current)
        chosen["evaluation_year"] = year
        chosen["training_rows"] = len(train)
        chosen["training_cutoff"] = start
        selected.append(chosen)
    return pd.concat(selected, ignore_index=True)


def metric(net: pd.Series) -> dict[str, float | int]:
    clean = net.dropna()
    if clean.empty:
        return {"trades": 0, "win_rate_pct": 0.0, "profit_factor": 0.0, "net_pct_points": 0.0, "max_drawdown_pct_points": 0.0}
    gains = clean.clip(lower=0.0).sum()
    losses = -clean.clip(upper=0.0).sum()
    cumulative = clean.cumsum()
    drawdown = cumulative - cumulative.cummax()
    return {
        "trades": int(len(clean)),
        "win_rate_pct": round(float(clean.gt(0.0).mean() * 100.0), 6),
        "profit_factor": round(float(gains / losses), 6) if losses else (999.0 if gains else 0.0),
        "net_pct_points": round(float(clean.sum() * 100.0), 6),
        "mean_net_bps": round(float(clean.mean() * 10_000.0), 6),
        "max_drawdown_pct_points": round(float(-drawdown.min() * 100.0), 6),
    }


def remove_top_winners(trades: pd.DataFrame, count: int = 3) -> pd.DataFrame:
    if trades.empty:
        return trades
    gross = trades.side * trades.future_return
    return trades.drop(index=gross.nlargest(min(count, len(trades))).index)


def summarize(trades: pd.DataFrame) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for year in EVALUATION_YEARS:
        scoped = trades.loc[trades.evaluation_year.eq(year)].copy()
        gross = scoped.side * scoped.future_return
        trimmed = remove_top_winners(scoped)
        trimmed_gross = trimmed.side * trimmed.future_return
        result[str(year)] = {
            "base": metric(gross - BASE_COST),
            "stress": metric(gross - STRESS_COST),
            "stress_without_top_three_winners": metric(trimmed_gross - STRESS_COST),
        }
    return result


def run(panel: pd.DataFrame) -> tuple[dict[str, Any], pd.DataFrame]:
    features = engineer_features(panel)
    selected = walk_forward(features)
    annual = summarize(selected)
    passes = all(
        annual[str(year)]["stress"]["trades"] >= 30
        and annual[str(year)]["stress"]["profit_factor"] > 1.0
        and annual[str(year)]["stress"]["net_pct_points"] > 0.0
        and annual[str(year)]["stress_without_top_three_winners"]["profit_factor"] > 1.0
        for year in EVALUATION_YEARS
    )
    report = {
        "experiment": "s0_daily_order_flow",
        "method": "Binance-only proxy for published world-order-flow research: completed UTC-day taker imbalance, nonlinear expanding-window cross-sectional forecast, next-day open entry, one-day hold, and one S0 position.",
        "limitations": [
            "The paper uses world order flow across eleven fiat currencies; Binance taker volume is only a venue-specific proxy.",
            "This screening stage uses open-to-open returns. Any passing candidate still requires an exact intraday stop/target path audit and fresh forward validation.",
            "The 2026 period has been inspected by earlier research and is not described as untouched blind data.",
        ],
        "features": list(FEATURES),
        "costs": {"base": BASE_COST, "stress": STRESS_COST},
        "selected_trades": int(len(selected)),
        "annual": annual,
        "decision": "screening_pass_requires_path_and_forward_validation" if passes else "rejected_not_positive_expectancy",
    }
    return report, selected


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.panel and args.panel.exists():
        panel = pd.read_parquet(args.panel)
    else:
        panel = build_daily_panel()
        panel.to_parquet(args.output / "daily_order_flow_panel.parquet", index=False)
    report, selected = run(panel)
    selected.to_parquet(args.output / "selected_trades.parquet", index=False)
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"experiment": report["experiment"], "selected_trades": report["selected_trades"], "decision": report["decision"], "annual": report["annual"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
