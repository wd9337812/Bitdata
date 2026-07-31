from __future__ import annotations

import argparse
import json
import math
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd


BASE_ROUND_TRIP_COST = 0.0012
STRESS_ROUND_TRIP_COST = 0.0024
ALPHA_IDS = (1, 2, 3, 5, 6, 9, 10, 11, 12, 13, 14, 18, 20, 22, 25, 30, 40, 41, 42, 43)
WINDOWS = {
    "development_2020_2022": ("2020-02-01", "2023-01-01"),
    "validation_2023": ("2023-01-01", "2024-01-01"),
    "test_2024": ("2024-01-01", "2025-01-01"),
    "blind_2025": ("2025-01-01", "2026-01-01"),
    "final_blind_2026": ("2026-01-01", "2027-01-01"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--history",
        type=Path,
        default=Path("data/research/binance_um_point_in_time_1h_2020_2023"),
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=Path("data/research/binance_um_point_in_time_1h_2024_2025"),
    )
    parser.add_argument(
        "--extension",
        type=Path,
        default=Path("data/research/binance_um_point_in_time_1h"),
    )
    parser.add_argument(
        "--output", type=Path, default=Path("data/research/s0_worldquant_superalpha")
    )
    parser.add_argument("--top-liquid", type=int, default=100)
    parser.add_argument("--workers", type=int, default=4)
    return parser.parse_args()


def parquet_path(root: Path, name: str) -> Path:
    return root / "parquet" / name


def available_names(roots: list[Path]) -> list[str]:
    return sorted(
        {
            path.name
            for root in roots
            for path in (root / "parquet").glob("*.parquet")
        }
    )


def load_daily_symbol(name: str, roots: list[Path]) -> pd.DataFrame:
    parts = []
    for root in roots:
        path = parquet_path(root, name)
        if path.exists():
            parts.append(pd.read_parquet(path))
    if not parts:
        return pd.DataFrame()
    hourly = (
        pd.concat(parts, ignore_index=True)
        .sort_values("open_time")
        .drop_duplicates("open_time", keep="last")
    )
    hourly["date"] = pd.to_datetime(hourly.open_time, unit="ms", utc=True).dt.floor("D")
    daily = hourly.groupby("date", as_index=False).agg(
        symbol=("symbol", "last"),
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        quote_volume=("quote_volume", "sum"),
        hourly_rows=("open_time", "count"),
    )
    return daily.loc[daily.hourly_rows.ge(20)].drop(columns="hourly_rows")


def build_panel(roots: list[Path], workers: int) -> pd.DataFrame:
    names = available_names(roots)
    parts: list[pd.DataFrame] = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(load_daily_symbol, name, roots): name for name in names}
        for completed, future in enumerate(as_completed(futures), start=1):
            frame = future.result()
            if not frame.empty:
                parts.append(frame)
            if completed % 50 == 0 or completed == len(futures):
                print(f"loaded daily symbols {completed}/{len(futures)}", flush=True)
    return pd.concat(parts, ignore_index=True).sort_values(["date", "symbol"])


def wide(panel: pd.DataFrame, field: str) -> pd.DataFrame:
    return panel.pivot(index="date", columns="symbol", values=field).sort_index()


def cs_rank(frame: pd.DataFrame, universe: pd.DataFrame) -> pd.DataFrame:
    return frame.where(universe).rank(axis=1, pct=True)


def rolling_covariance(left: pd.DataFrame, right: pd.DataFrame, window: int) -> pd.DataFrame:
    return (left * right).rolling(window).mean() - left.rolling(window).mean() * right.rolling(window).mean()


def rolling_correlation(left: pd.DataFrame, right: pd.DataFrame, window: int) -> pd.DataFrame:
    left_std = left.rolling(window).std(ddof=0)
    right_std = right.rolling(window).std(ddof=0)
    result = rolling_covariance(left, right, window) / (left_std * right_std)
    return result.mask(left_std.eq(0) | right_std.eq(0))


def ts_argmax_age(values: np.ndarray) -> float:
    if np.isnan(values).any():
        return np.nan
    return float(len(values) - 1 - np.argmax(values))


def compute_alphas(panel: pd.DataFrame, universe: pd.DataFrame) -> dict[int, pd.DataFrame]:
    open_ = wide(panel, "open")
    high = wide(panel, "high")
    low = wide(panel, "low")
    close = wide(panel, "close")
    volume = wide(panel, "volume")
    vwap = (close + high + low) / 3.0
    returns = close.pct_change(fill_method=None)

    alphas: dict[int, pd.DataFrame] = {}
    std20 = returns.rolling(20).std(ddof=0)
    expr = std20.where(returns.lt(0), close)
    powered = np.sign(expr) * expr.pow(2)
    alphas[1] = cs_rank(powered.rolling(5).apply(ts_argmax_age, raw=True), universe) - 0.5

    rank_volume_delta = cs_rank(np.log(volume).diff(2), universe)
    rank_intraday = cs_rank((close - open_) / open_, universe)
    alphas[2] = -rolling_correlation(rank_volume_delta, rank_intraday, 6)
    alphas[3] = -rolling_correlation(cs_rank(open_, universe), cs_rank(volume, universe), 10)
    alphas[5] = cs_rank(open_ - vwap.rolling(10).mean(), universe) * -cs_rank(close - vwap, universe).abs()
    alphas[6] = -rolling_correlation(open_, volume, 10)

    delta = close.diff()
    for alpha_id, lookback in ((9, 5), (10, 4)):
        minimum = delta.rolling(lookback).min()
        maximum = delta.rolling(lookback).max()
        inner = delta.where(maximum.lt(0), -delta)
        raw = delta.where(minimum.gt(0), inner)
        alphas[alpha_id] = cs_rank(raw, universe) if alpha_id == 10 else raw

    diff_vwap = vwap - close
    alphas[11] = (
        cs_rank(diff_vwap.rolling(3).max(), universe)
        + cs_rank(diff_vwap.rolling(3).min(), universe)
    ) * cs_rank(volume.diff(3), universe)
    alphas[12] = np.sign(volume.diff()) * -close.diff()
    close_rank = cs_rank(close, universe)
    volume_rank = cs_rank(volume, universe)
    alphas[13] = -cs_rank(rolling_covariance(close_rank, volume_rank, 5), universe)
    alphas[14] = -cs_rank(returns.diff(3), universe) * rolling_correlation(open_, volume, 10)

    intraday = close - open_
    alphas[18] = -cs_rank(
        intraday.abs().rolling(5).std(ddof=0)
        + intraday
        + rolling_correlation(close, open_, 10),
        universe,
    )
    alphas[20] = (
        -cs_rank(open_ - high.shift(1), universe)
        * cs_rank(open_ - close.shift(1), universe)
        * cs_rank(open_ - low.shift(1), universe)
    )
    alphas[22] = -rolling_correlation(high, volume, 5).diff(5) * cs_rank(
        close.rolling(20).std(ddof=0), universe
    )
    alphas[25] = cs_rank(
        -returns * volume.rolling(20).mean() * vwap * (high - close), universe
    )
    direction_sum = np.sign(close.diff()) + np.sign(close.shift(1) - close.shift(2)) + np.sign(close.shift(2) - close.shift(3))
    alphas[30] = (1.0 - cs_rank(direction_sum, universe)) * volume.rolling(5).sum() / volume.rolling(20).sum()
    alphas[40] = -cs_rank(high.rolling(10).std(ddof=0), universe) * rolling_correlation(high, volume, 10)
    alphas[41] = (high * low).pow(0.5) - vwap
    alphas[42] = cs_rank(vwap - close, universe) / cs_rank(vwap + close, universe)
    alphas[43] = (
        (volume / volume.rolling(20).mean()).rolling(20).rank(pct=True)
        * (-close.diff(7)).rolling(8).rank(pct=True)
    )
    return {alpha_id: frame.where(universe) for alpha_id, frame in alphas.items()}


def daily_ic(alpha: pd.DataFrame, target: pd.DataFrame, mask: pd.DataFrame) -> pd.Series:
    values = []
    for date in alpha.index:
        valid = mask.loc[date] & alpha.loc[date].notna() & target.loc[date].notna()
        if valid.sum() < 10:
            values.append(np.nan)
            continue
        left = alpha.loc[date, valid]
        right = target.loc[date, valid]
        values.append(left.corr(right))
    return pd.Series(values, index=alpha.index, dtype=float)


def ic_summary(values: pd.Series) -> dict[str, float | int]:
    valid = values.dropna()
    std = float(valid.std(ddof=0))
    mean = float(valid.mean())
    t_stat = mean * math.sqrt(len(valid)) / std if std > 0 else 0.0
    return {"days": int(len(valid)), "mean": mean, "std": std, "t_stat": t_stat}


def rank_normalize(alpha: pd.DataFrame, universe: pd.DataFrame) -> pd.DataFrame:
    return cs_rank(alpha, universe) - 0.5


def train_superalpha(
    alphas: dict[int, pd.DataFrame],
    target: pd.DataFrame,
    universe: pd.DataFrame,
    start: str,
    end: str,
) -> tuple[pd.DataFrame, dict[int, dict[str, float | int]], dict[int, float]]:
    train = universe.index.to_series().between(start, end, inclusive="left")
    summaries: dict[int, dict[str, float | int]] = {}
    weights: dict[int, float] = {}
    for alpha_id in ALPHA_IDS:
        ic = daily_ic(alphas[alpha_id], target, universe)
        summary = ic_summary(ic.loc[train.values])
        summaries[alpha_id] = summary
        if abs(float(summary["t_stat"])) >= 1.0:
            weights[alpha_id] = float(summary["mean"])
    if not weights:
        raise RuntimeError("No alpha passed the preregistered training IC threshold")
    denominator = sum(abs(weight) for weight in weights.values())
    score = sum(
        weight * rank_normalize(alphas[alpha_id], universe)
        for alpha_id, weight in weights.items()
    ) / denominator
    return score.where(universe), summaries, weights


def build_s0_candidates(
    score: pd.DataFrame,
    target: pd.DataFrame,
    universe: pd.DataFrame,
) -> pd.DataFrame:
    records = []
    for date in score.index:
        valid = universe.loc[date] & score.loc[date].notna() & target.loc[date].notna()
        if valid.sum() < 10:
            continue
        row = score.loc[date, valid]
        symbol = row.abs().idxmax()
        raw_score = float(row[symbol])
        gross = float(np.sign(raw_score) * target.loc[date, symbol])
        records.append(
            {
                "signal_date": date,
                "entry_date": date + pd.Timedelta(days=1),
                "symbol": symbol,
                "direction": "LONG" if raw_score > 0 else "SHORT",
                "score": raw_score,
                "confidence": abs(raw_score),
                "gross_return": gross,
            }
        )
    return pd.DataFrame(records)


def choose_confidence_threshold(candidates: pd.DataFrame) -> tuple[float, list[dict[str, float | int]]]:
    development = candidates.loc[
        candidates.signal_date.ge("2020-02-01") & candidates.signal_date.lt("2023-01-01")
    ]
    validation = candidates.loc[
        candidates.signal_date.ge("2023-01-01") & candidates.signal_date.lt("2024-01-01")
    ]
    thresholds = sorted(
        {
            0.0,
            *[float(development.confidence.quantile(q)) for q in (0.50, 0.75, 0.90)],
        }
    )
    rows = []
    for threshold in thresholds:
        selected = validation.loc[validation.confidence.ge(threshold)].copy()
        selected["net_return"] = selected.gross_return - BASE_ROUND_TRIP_COST
        metric = trade_metrics(selected.net_return)
        rows.append({"threshold": threshold, **metric})
    eligible = [row for row in rows if int(row["trades"]) >= 30]
    chosen = max(eligible, key=lambda row: (float(row["net_return"]), float(row["profit_factor"])))
    return float(chosen["threshold"]), rows


def trade_metrics(returns: pd.Series) -> dict[str, float | int]:
    clean = returns.dropna().astype(float)
    wins = clean.loc[clean.gt(0)].sum()
    losses = -clean.loc[clean.lt(0)].sum()
    wealth = (1.0 + clean.clip(lower=-0.999)).cumprod()
    drawdown = wealth / wealth.cummax() - 1.0 if not wealth.empty else pd.Series(dtype=float)
    return {
        "trades": int(len(clean)),
        "win_rate": float(clean.gt(0).mean()) if len(clean) else 0.0,
        "profit_factor": float(wins / losses) if losses > 0 else 999.0 if wins > 0 else 0.0,
        "net_return": float(clean.sum()),
        "compounded_return": float(wealth.iloc[-1] - 1.0) if len(wealth) else 0.0,
        "max_drawdown": float(drawdown.min()) if len(drawdown) else 0.0,
    }


def window_metrics(trades: pd.DataFrame, cost: float) -> dict[str, dict[str, float | int]]:
    output = {}
    for name, (start, end) in WINDOWS.items():
        subset = trades.loc[
            trades.signal_date.ge(start) & trades.signal_date.lt(end)
        ].copy()
        output[name] = trade_metrics(subset.gross_return - cost)
    return output


def concentration_metrics(trades: pd.DataFrame) -> dict[str, dict[str, object]]:
    output: dict[str, dict[str, object]] = {}
    for name, (start, end) in WINDOWS.items():
        subset = trades.loc[
            trades.signal_date.ge(start) & trades.signal_date.lt(end)
        ].copy()
        subset["net_return"] = subset.gross_return - BASE_ROUND_TRIP_COST
        if subset.empty:
            output[name] = {"best_trade": None, "without_best_trade": trade_metrics(pd.Series(dtype=float))}
            continue
        best_index = subset.net_return.idxmax()
        best = subset.loc[best_index]
        output[name] = {
            "best_trade": {
                "symbol": str(best.symbol),
                "net_return": float(best.net_return),
            },
            "without_best_trade": trade_metrics(subset.drop(index=best_index).net_return),
        }
    return output


def main() -> None:
    args = parse_args()
    roots = [args.history, args.data, args.extension]
    panel = build_panel(roots, args.workers)
    close = wide(panel, "close")
    open_ = wide(panel, "open")
    quote_volume = wide(panel, "quote_volume")
    trailing_liquidity = quote_volume.rolling(30, min_periods=20).sum()
    universe = (
        trailing_liquidity.rank(axis=1, ascending=False, method="first").le(args.top_liquid)
        & close.notna().cumsum().ge(30)
    )
    target = (close.shift(-1) - open_.shift(-1)) / open_.shift(-1)
    alphas = compute_alphas(panel, universe)
    score, alpha_summaries, weights = train_superalpha(
        alphas, target, universe, "2020-02-01", "2023-01-01"
    )
    candidates = build_s0_candidates(score, target, universe)
    threshold, threshold_validation = choose_confidence_threshold(candidates)
    trades = candidates.loc[candidates.confidence.ge(threshold)].copy()
    base = window_metrics(trades, BASE_ROUND_TRIP_COST)
    stress = window_metrics(trades, STRESS_ROUND_TRIP_COST)
    concentration = concentration_metrics(trades)
    oos = ("test_2024", "blind_2025", "final_blind_2026")
    accepted = bool(
        all(base[name]["trades"] >= 20 for name in oos)
        and all(base[name]["net_return"] > 0 for name in oos)
        and all(stress[name]["net_return"] > 0 for name in oos)
        and all(base[name]["profit_factor"] >= 1.10 for name in oos)
        and all(concentration[name]["without_best_trade"]["net_return"] > 0 for name in oos)
    )
    report = {
        "experiment": "s0_worldquant_superalpha_point_in_time_audit",
        "source": "WorldQuant 101 alpha formulas and public crypto super-alpha repository",
        "source_audit": {
            "original_limitations": [
                "fixed 43-coin survivor universe",
                "portfolio holdings rather than S0 single-position execution",
                "only one reported out-of-sample calendar year",
            ],
            "corrections": [
                "point-in-time Binance USD-M universe",
                "prior 30-day quote-volume liquidity rank",
                "single strongest absolute score per day",
                "next-day open-to-close execution target",
            ],
        },
        "training": {
            "alpha_ic_window": "2020-02 through 2022-12",
            "threshold_validation": "2023",
            "selected_alpha_weights": {str(k): v for k, v in weights.items()},
            "alpha_ic": {str(k): v for k, v in alpha_summaries.items()},
            "selected_confidence_threshold": threshold,
            "threshold_validation_results": threshold_validation,
        },
        "symbols": int(panel.symbol.nunique()),
        "candidate_days": int(len(candidates)),
        "selected_trades": int(len(trades)),
        "base_round_trip_cost": BASE_ROUND_TRIP_COST,
        "stress_round_trip_cost": STRESS_ROUND_TRIP_COST,
        "base": base,
        "stress": stress,
        "concentration": concentration,
        "accepted": accepted,
        "decision": "accepted_for_minute_path_validation" if accepted else "rejected_not_positive_expectancy",
    }
    args.output.mkdir(parents=True, exist_ok=True)
    trades.to_parquet(args.output / "s0_trades.parquet", index=False)
    score.stack().rename("score").reset_index().to_parquet(
        args.output / "daily_scores.parquet", index=False
    )
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
