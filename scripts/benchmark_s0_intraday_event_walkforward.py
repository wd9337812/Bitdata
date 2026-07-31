from __future__ import annotations

import argparse
import json
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd


BASE_COST = 0.0012
STRESS_COST = 0.0024
HOLD_HOURS = 24
STOP_PCT = 0.05
TAKE_PCT = 0.08
MIN_LIQUIDITY_24H = 20_000_000.0
MIN_AGE_DAYS = 60
EVALUATION_YEARS = (2023, 2024, 2025, 2026)
QUANTILES = (0.90, 0.95, 0.975, 0.99)
FEATURES = (
    "ret_1h",
    "ret_3h",
    "ret_6h",
    "ret_12h",
    "ret_24h",
    "ret_72h",
    "vol_6h",
    "vol_24h",
    "atr_pct",
    "range_pct",
    "volume_ratio_6h",
    "volume_ratio_24h",
    "age_days",
    "breadth_24h",
    "cs_ret_3h",
    "cs_ret_6h",
    "cs_ret_12h",
    "cs_ret_24h",
    "cs_ret_72h",
    "cs_vol_6h",
    "cs_vol_24h",
    "cs_atr_pct",
    "cs_volume_ratio_6h",
    "cs_liquidity_24h",
    "btc_ret_3h",
    "btc_ret_6h",
    "btc_ret_12h",
    "btc_ret_24h",
    "btc_ret_72h",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
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
        "--output",
        type=Path,
        default=Path("data/research/s0_intraday_event_walkforward"),
    )
    parser.add_argument("--rebuild-panel", action="store_true")
    return parser.parse_args()


def path_return(
    entry: np.ndarray,
    highs: list[np.ndarray],
    lows: list[np.ndarray],
    terminal: np.ndarray,
    direction: int,
) -> np.ndarray:
    result = np.full(len(entry), np.nan, dtype=float)
    active = np.isfinite(entry)
    for high, low in zip(highs, lows, strict=True):
        if direction > 0:
            take = high >= entry * (1.0 + TAKE_PCT)
            stop = low <= entry * (1.0 - STOP_PCT)
        else:
            take = low <= entry * (1.0 - TAKE_PCT)
            stop = high >= entry * (1.0 + STOP_PCT)
        stopped = active & stop
        won = active & take & ~stop
        result[stopped] = -STOP_PCT
        result[won] = TAKE_PCT
        active &= ~(stopped | won)
    result[active] = direction * (terminal[active] / entry[active] - 1.0)
    return result


def symbol_features(frame: pd.DataFrame, start_ms: int) -> pd.DataFrame:
    frame = frame.sort_values("open_time").drop_duplicates("open_time").copy()
    previous = frame.close.shift(1)
    true_range = pd.concat(
        [
            frame.high - frame.low,
            (frame.high - previous).abs(),
            (frame.low - previous).abs(),
        ],
        axis=1,
    ).max(axis=1)
    hourly_return = frame.close.pct_change(fill_method=None)
    for hours in (1, 3, 6, 12, 24, 72):
        frame[f"ret_{hours}h"] = frame.close.pct_change(hours, fill_method=None)
    frame["vol_6h"] = hourly_return.rolling(6).std()
    frame["vol_24h"] = hourly_return.rolling(24).std()
    frame["atr_pct"] = true_range.rolling(24).mean() / frame.close
    frame["range_pct"] = (frame.high - frame.low) / frame.close
    frame["liquidity_24h"] = frame.quote_volume.rolling(24).sum()
    frame["volume_ratio_6h"] = frame.quote_volume / frame.quote_volume.rolling(6).mean()
    frame["volume_ratio_24h"] = frame.quote_volume / frame.quote_volume.rolling(24).mean()
    frame["available_ms"] = frame.open_time.astype("int64") + 3_600_000
    frame["age_days"] = (frame.available_ms - int(start_ms)) / 86_400_000

    entry = frame.open.shift(-1).to_numpy()
    highs = [frame.high.shift(-step).to_numpy() for step in range(1, HOLD_HOURS + 1)]
    lows = [frame.low.shift(-step).to_numpy() for step in range(1, HOLD_HOURS + 1)]
    terminal = frame.close.shift(-HOLD_HOURS).to_numpy()
    frame["long_gross"] = path_return(entry, highs, lows, terminal, 1)
    frame["short_gross"] = path_return(entry, highs, lows, terminal, -1)
    frame["label_exit_ms"] = frame.available_ms + HOLD_HOURS * 3_600_000
    entry_time = frame.open_time.shift(-1)
    terminal_time = frame.open_time.shift(-HOLD_HOURS)
    continuous = entry_time.eq(frame.open_time + 3_600_000) & terminal_time.eq(
        frame.open_time + HOLD_HOURS * 3_600_000
    )
    frame.loc[~continuous, ["long_gross", "short_gross"]] = np.nan
    # Four-hour decisions materially reduce panel size and REST-equivalent work.
    return frame.loc[(frame.available_ms // 3_600_000).mod(4).eq(0)].copy()


def load_symbol(path_by_root: list[Path], symbol: str) -> pd.DataFrame:
    columns = ["symbol", "open_time", "open", "high", "low", "close", "quote_volume"]
    parts = []
    for root in path_by_root:
        path = root / "parquet" / f"{symbol}.parquet"
        if path.exists():
            parts.append(pd.read_parquet(path, columns=columns))
    if not parts:
        return pd.DataFrame(columns=columns)
    return pd.concat(parts, ignore_index=True)


def build_panel(roots: list[Path]) -> pd.DataFrame:
    symbols = sorted(
        {path.stem for root in roots for path in (root / "parquet").glob("*.parquet")}
    )
    parts = []
    for number, symbol in enumerate(symbols, start=1):
        raw = load_symbol(roots, symbol)
        if raw.empty:
            continue
        start_ms = int(raw.open_time.min())
        parts.append(symbol_features(raw, start_ms))
        if number % 100 == 0:
            print(f"featured {number}/{len(symbols)} symbols")
    panel = pd.concat(parts, ignore_index=True)
    panel = panel.loc[
        panel.age_days.ge(MIN_AGE_DAYS)
        & panel.liquidity_24h.ge(MIN_LIQUIDITY_24H)
    ].copy()
    for column in (
        "ret_3h",
        "ret_6h",
        "ret_12h",
        "ret_24h",
        "ret_72h",
        "vol_6h",
        "vol_24h",
        "atr_pct",
        "volume_ratio_6h",
        "liquidity_24h",
    ):
        panel[f"cs_{column}"] = panel.groupby("available_ms")[column].rank(pct=True)
    panel["breadth_24h"] = panel.groupby("available_ms").ret_24h.transform(
        lambda values: float(values.gt(0).mean())
    )
    btc = (
        panel.loc[
            panel.symbol.eq("BTCUSDT"),
            ["available_ms", "ret_3h", "ret_6h", "ret_12h", "ret_24h", "ret_72h"],
        ]
        .drop_duplicates("available_ms")
        .set_index("available_ms")
        .add_prefix("btc_")
    )
    panel = panel.merge(btc, left_on="available_ms", right_index=True, how="left")
    long_net = panel.long_gross - BASE_COST
    short_net = panel.short_gross - BASE_COST
    panel["target"] = np.select(
        [(long_net > 0) & (long_net >= short_net), (short_net > 0) & (short_net > long_net)],
        [1, 2],
        default=0,
    ).astype("int8")
    return panel.dropna(subset=[*FEATURES, "long_gross", "short_gross"]).reset_index(drop=True)


def classifier() -> lgb.LGBMClassifier:
    return lgb.LGBMClassifier(
        objective="multiclass",
        num_class=3,
        n_estimators=300,
        learning_rate=0.03,
        num_leaves=15,
        max_depth=5,
        min_child_samples=500,
        subsample=0.75,
        subsample_freq=1,
        colsample_bytree=0.75,
        reg_alpha=1.0,
        reg_lambda=6.0,
        class_weight="balanced",
        random_state=20260801,
        n_jobs=4,
        verbosity=-1,
    )


def score_year(panel: pd.DataFrame, year: int) -> pd.DataFrame:
    cutoff = int((pd.Timestamp(f"{year}-01-01", tz="UTC") - pd.Timedelta(hours=25)).timestamp() * 1000)
    train = panel.loc[
        panel.available_ms.ge(int(pd.Timestamp("2020-04-01", tz="UTC").timestamp() * 1000))
        & panel.label_exit_ms.lt(cutoff)
    ]
    current = panel.loc[pd.to_datetime(panel.available_ms, unit="ms", utc=True).dt.year.eq(year)].copy()
    estimator = classifier()
    estimator.fit(train.loc[:, FEATURES].astype("float32"), train.target)
    probabilities = estimator.predict_proba(current.loc[:, FEATURES].astype("float32"))
    current["p_none"] = probabilities[:, 0]
    current["p_long"] = probabilities[:, 1]
    current["p_short"] = probabilities[:, 2]
    current["confidence"] = np.maximum(current.p_long, current.p_short)
    current["margin"] = np.abs(current.p_long - current.p_short)
    current["side"] = np.where(current.p_long >= current.p_short, 1, -1)
    current["evaluation_year"] = year
    current["train_rows"] = len(train)
    return current


def select_trades(scored: pd.DataFrame, quantile: float) -> pd.DataFrame:
    threshold = float(scored.confidence.quantile(quantile))
    candidates = scored.loc[
        scored.confidence.ge(threshold)
        & scored.margin.ge(0.05)
        & scored.confidence.gt(scored.p_none)
    ].copy()
    candidates = (
        candidates.sort_values(
            ["available_ms", "confidence", "liquidity_24h"],
            ascending=[True, False, False],
        )
        .drop_duplicates("available_ms")
        .sort_values("available_ms")
    )
    selected = []
    free_at = -1
    for row in candidates.itertuples():
        if row.available_ms >= free_at:
            selected.append(row.Index)
            free_at = row.available_ms + HOLD_HOURS * 3_600_000
    trades = candidates.loc[selected].copy()
    trades["gross_return"] = np.where(
        trades.side.gt(0), trades.long_gross, trades.short_gross
    )
    trades["confidence_quantile"] = quantile
    trades["confidence_threshold"] = threshold
    return trades.reset_index(drop=True)


def metrics(values: pd.Series) -> dict[str, float | int]:
    values = values.dropna().astype(float)
    gains = float(values.loc[values.gt(0)].sum())
    losses = float(-values.loc[values.lt(0)].sum())
    equity = values.add(1.0).clip(lower=0.001).cumprod()
    drawdown = equity.div(equity.cummax()).sub(1.0)
    return {
        "trades": int(len(values)),
        "win_rate": float(values.gt(0).mean()) if len(values) else 0.0,
        "profit_factor": gains / losses if losses else (999.0 if gains else 0.0),
        "net_return": float(values.sum()),
        "mean_return": float(values.mean()) if len(values) else 0.0,
        "max_drawdown": float(drawdown.min()) if len(drawdown) else 0.0,
    }


def evaluate(trades: pd.DataFrame) -> dict[str, dict[str, float | int]]:
    return {
        "base": metrics(trades.gross_return - BASE_COST),
        "stress": metrics(trades.gross_return - STRESS_COST),
    }


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    panel_path = args.output / "four_hour_panel.parquet"
    if args.rebuild_panel or not panel_path.exists():
        panel = build_panel(args.roots)
        panel.to_parquet(panel_path, index=False)
    else:
        panel = pd.read_parquet(panel_path)

    validation_scored = score_year(panel, 2022)
    validation = {}
    for quantile in QUANTILES:
        trades = select_trades(validation_scored, quantile)
        validation[str(quantile)] = evaluate(trades)
    qualified = [
        quantile
        for quantile in QUANTILES
        if validation[str(quantile)]["stress"]["trades"] >= 30
        and validation[str(quantile)]["stress"]["profit_factor"] >= 1.10
        and validation[str(quantile)]["stress"]["net_return"] > 0
    ]
    frozen = (
        max(
            qualified,
            key=lambda value: (
                validation[str(value)]["stress"]["profit_factor"],
                validation[str(value)]["stress"]["net_return"],
            ),
        )
        if qualified
        else None
    )
    annual = {}
    all_trades = []
    if frozen is not None:
        for year in EVALUATION_YEARS:
            trades = select_trades(score_year(panel, year), frozen)
            annual[str(year)] = evaluate(trades)
            all_trades.append(trades)
    combined = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    combined_report = evaluate(combined) if len(combined) else {"base": metrics(pd.Series(dtype=float)), "stress": metrics(pd.Series(dtype=float))}
    remove_top_three = None
    if len(combined):
        stressed = combined.gross_return - STRESS_COST
        remove_top_three = metrics(stressed.drop(stressed.nlargest(3).index))
    accepted = bool(
        frozen is not None
        and all(item["stress"]["trades"] >= 20 for item in annual.values())
        and all(item["stress"]["profit_factor"] >= 1.10 for item in annual.values())
        and all(item["stress"]["net_return"] > 0 for item in annual.values())
        and remove_top_three is not None
        and remove_top_three["net_return"] > 0
    )
    report = {
        "experiment": "s0_intraday_event_walkforward",
        "method": "Four-hour point-in-time ranking with next-hour entry and conservative 24-hour 5% stop / 8% take labels.",
        "panel_rows": int(len(panel)),
        "symbols": int(panel.symbol.nunique()),
        "validation_2022": validation,
        "frozen_confidence_quantile": frozen,
        "annual_out_of_sample": annual,
        "combined": combined_report,
        "combined_without_top_three_winners": remove_top_three,
        "accepted": accepted,
        "live_qualified": False,
        "decision": "forward_shadow_required" if accepted else "rejected_not_stable_positive_expectancy",
    }
    if len(combined):
        combined.to_parquet(args.output / "frozen_trades.parquet", index=False)
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
