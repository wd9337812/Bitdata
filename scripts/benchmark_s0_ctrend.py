from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
from sklearn.linear_model import ElasticNet


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_s0_donchian_ensemble import (  # noqa: E402
    ONE_WAY_COST,
)
from scripts.benchmark_s0_volume_weighted_tsmom import (  # noqa: E402
    STABLE_BASES,
    base_asset,
)
from scripts.benchmark_s0_xmom_point_in_time import load_manifest  # noqa: E402


DEFAULT_DATA = (
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2024_2025"
)
DEFAULT_AUDIT_DATA = (
    ROOT / "data" / "research" / "binance_um_point_in_time_1h"
)
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_ctrend"
TRAINING_WEEKS = 52
MINIMUM_TRAINING_ROWS = 2_000
MINIMUM_SYMBOL_AGE_DAYS = 200
MINIMUM_MEDIAN_DAILY_VOLUME = 1_000_000.0
ONE_WAY_STRESS_COST = 0.0012
ALPHA_GRID = np.logspace(-5, -2, 7)
SMA_WINDOWS = (3, 5, 10, 20, 50, 100, 200)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Point-in-time adaptation of the published CTREND technical "
            "indicator factor, including directly executable S0 variants."
        )
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--audit-data", type=Path, default=DEFAULT_AUDIT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def _safe_ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    return numerator / denominator.replace(0.0, np.nan)


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    change = close.diff()
    gain = change.clip(lower=0).rolling(period, min_periods=period).mean()
    loss = -change.clip(upper=0).rolling(period, min_periods=period).mean()
    relative_strength = _safe_ratio(gain, loss)
    return 100.0 - 100.0 / (1.0 + relative_strength)


def _stochastic(close: pd.Series, low: pd.Series, high: pd.Series) -> pd.Series:
    lowest = low.rolling(14, min_periods=14).min()
    highest = high.rolling(14, min_periods=14).max()
    return 100.0 * _safe_ratio(close - lowest, highest - lowest)


def _cci(close: pd.Series, low: pd.Series, high: pd.Series) -> pd.Series:
    typical = (high + low + close) / 3.0
    mean = typical.rolling(20, min_periods=20).mean()
    deviation = typical.rolling(20, min_periods=20).apply(
        lambda values: float(np.mean(np.abs(values - np.mean(values)))), raw=True
    )
    return _safe_ratio(typical - mean, 0.015 * deviation)


def _chaikin_money_flow(
    close: pd.Series,
    low: pd.Series,
    high: pd.Series,
    volume: pd.Series,
) -> pd.Series:
    multiplier = _safe_ratio((close - low) - (high - close), high - low)
    money_flow = multiplier.fillna(0.0) * volume
    return _safe_ratio(
        money_flow.rolling(20, min_periods=20).sum(),
        volume.rolling(20, min_periods=20).sum(),
    )


def indicator_columns() -> list[str]:
    columns = [
        "rsi_14",
        "stoch_k_14",
        "stoch_d_3",
        "stoch_rsi_14",
        "cci_20",
    ]
    columns += [f"price_sma_{window}" for window in SMA_WINDOWS]
    columns += ["price_macd", "price_macd_signal_gap"]
    columns += [f"volume_sma_{window}" for window in SMA_WINDOWS]
    columns += ["volume_macd", "volume_macd_signal_gap", "chaikin_money_flow"]
    columns += [
        "bollinger_lower",
        "bollinger_middle",
        "bollinger_upper",
        "bollinger_width",
    ]
    return columns


def daily_symbol_features(paths: Path | Iterable[Path], start_ms: int) -> pd.DataFrame:
    if isinstance(paths, Path):
        paths = [paths]
    columns = [
        "symbol",
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "quote_volume",
    ]
    source = (
        pd.concat(
            [pd.read_parquet(path, columns=columns) for path in paths],
            ignore_index=True,
        )
        .sort_values("open_time")
        .drop_duplicates("open_time", keep="last")
    )
    source["day"] = pd.to_datetime(
        source.open_time, unit="ms", utc=True
    ).dt.floor("D")
    frame = (
        source.groupby(["symbol", "day"], as_index=False, sort=True)
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            quote_volume=("quote_volume", "sum"),
        )
        .sort_values("day")
        .reset_index(drop=True)
    )
    close = frame.close
    volume = frame.quote_volume
    stochastic = _stochastic(close, frame.low, frame.high)
    frame["rsi_14"] = _rsi(close)
    frame["stoch_k_14"] = stochastic
    frame["stoch_d_3"] = stochastic.rolling(3, min_periods=3).mean()
    rsi_low = frame.rsi_14.rolling(14, min_periods=14).min()
    rsi_high = frame.rsi_14.rolling(14, min_periods=14).max()
    frame["stoch_rsi_14"] = _safe_ratio(frame.rsi_14 - rsi_low, rsi_high - rsi_low)
    frame["cci_20"] = _cci(close, frame.low, frame.high)
    for window in SMA_WINDOWS:
        frame[f"price_sma_{window}"] = _safe_ratio(
            close.rolling(window, min_periods=window).mean(), close
        )
        frame[f"volume_sma_{window}"] = _safe_ratio(
            volume.rolling(window, min_periods=window).mean(), volume
        )
    price_macd = close.ewm(span=12, adjust=False).mean() - close.ewm(
        span=26, adjust=False
    ).mean()
    frame["price_macd"] = _safe_ratio(price_macd, close)
    frame["price_macd_signal_gap"] = _safe_ratio(
        price_macd - price_macd.ewm(span=9, adjust=False).mean(), close
    )
    volume_macd = volume.ewm(span=12, adjust=False).mean() - volume.ewm(
        span=26, adjust=False
    ).mean()
    frame["volume_macd"] = _safe_ratio(volume_macd, volume)
    frame["volume_macd_signal_gap"] = _safe_ratio(
        volume_macd - volume_macd.ewm(span=9, adjust=False).mean(), volume
    )
    frame["chaikin_money_flow"] = _chaikin_money_flow(
        close, frame.low, frame.high, volume
    )
    middle = close.rolling(20, min_periods=20).mean()
    deviation = close.rolling(20, min_periods=20).std()
    lower = middle - 2.0 * deviation
    upper = middle + 2.0 * deviation
    frame["bollinger_lower"] = _safe_ratio(lower, close)
    frame["bollinger_middle"] = _safe_ratio(middle, close)
    frame["bollinger_upper"] = _safe_ratio(upper, close)
    frame["bollinger_width"] = _safe_ratio(upper - lower, middle)
    frame["median_daily_volume_30d"] = volume.rolling(
        30, min_periods=20
    ).median()
    listed_at = pd.to_datetime(int(start_ms), unit="ms", utc=True)
    frame["symbol_age_days"] = (
        frame.day - listed_at
    ).dt.total_seconds() / 86_400.0
    return frame


def load_daily_panel(paths: Iterable[Path]) -> tuple[pd.DataFrame, dict[str, Any]]:
    per_symbol: dict[str, list[Path]] = {}
    manifests: list[dict[str, Any]] = []
    starts: dict[str, int] = {}
    for data in paths:
        if not data.exists():
            continue
        source_starts, manifest = load_manifest(data)
        manifests.append(manifest)
        for symbol, start in source_starts.items():
            starts[symbol] = min(starts.get(symbol, start), start)
        for path in sorted((data / "parquet").glob("*.parquet")):
            symbol = path.stem
            if base_asset(symbol) in STABLE_BASES or symbol not in source_starts:
                continue
            per_symbol.setdefault(symbol, []).append(path)
    frames: list[pd.DataFrame] = []
    for symbol, symbol_paths in per_symbol.items():
        frames.append(daily_symbol_features(symbol_paths, starts[symbol]))
    if not frames:
        raise ValueError("No point-in-time source files found")
    panel = pd.concat(frames, ignore_index=True).sort_values(["day", "symbol"])
    return panel.reset_index(drop=True), {"manifests": manifests}


def weekly_panel(daily: pd.DataFrame) -> pd.DataFrame:
    frame = daily.copy()
    naive_day = frame.day.dt.tz_localize(None)
    frame["week"] = naive_day.dt.to_period("W-SUN").dt.end_time.dt.tz_localize("UTC")
    weekly = frame.groupby(["symbol", "week"], sort=True).tail(1).copy()
    weekly["next_week_close"] = weekly.groupby("symbol").close.shift(-1)
    weekly["next_week"] = weekly.groupby("symbol").week.shift(-1)
    weekly["next_return"] = weekly.next_week_close / weekly.close - 1.0
    contiguous = weekly.next_week.sub(weekly.week).eq(pd.Timedelta(days=7))
    weekly.loc[~contiguous, "next_return"] = np.nan
    eligible = weekly.loc[
        weekly.symbol_age_days.ge(MINIMUM_SYMBOL_AGE_DAYS)
        & weekly.median_daily_volume_30d.ge(MINIMUM_MEDIAN_DAILY_VOLUME)
        & weekly.next_return.notna()
    ].copy()
    features = indicator_columns()
    eligible = eligible.dropna(subset=features)
    for column in features:
        percentile = eligible.groupby("week")[column].rank(pct=True, method="average")
        eligible[f"rank_{column}"] = percentile - 0.5
    return eligible.reset_index(drop=True)


def _weighted_aicc(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    sample_weight: np.ndarray,
    parameters: int,
) -> float:
    residual = y_true - y_pred
    rss = float(np.sum(sample_weight * residual * residual))
    n = int(len(y_true))
    if rss <= 0 or n <= parameters + 1:
        return float("inf")
    aic = n * math.log(rss / n) + 2 * parameters
    return aic + 2 * parameters * (parameters + 1) / (n - parameters - 1)


def fit_positive_elastic_net(
    training: pd.DataFrame,
    feature_columns: list[str],
) -> tuple[ElasticNet, float]:
    clean = training.dropna(subset=feature_columns + ["next_return"]).copy()
    if len(clean) < MINIMUM_TRAINING_ROWS:
        raise ValueError("Insufficient rolling training rows")
    x = clean[feature_columns].to_numpy(dtype="float64")
    y = clean.next_return.to_numpy(dtype="float64")
    liquidity = np.sqrt(clean.median_daily_volume_30d.to_numpy(dtype="float64"))
    sample_weight = liquidity / np.nanmean(liquidity)
    best: tuple[float, ElasticNet, float] | None = None
    for alpha in ALPHA_GRID:
        model = ElasticNet(
            alpha=float(alpha),
            l1_ratio=0.5,
            positive=True,
            fit_intercept=True,
            max_iter=20_000,
            tol=1e-5,
            random_state=42,
        )
        model.fit(x, y, sample_weight=sample_weight)
        prediction = model.predict(x)
        parameters = int(np.count_nonzero(model.coef_)) + 1
        score = _weighted_aicc(y, prediction, sample_weight, parameters)
        if best is None or score < best[0]:
            best = (score, model, float(alpha))
    if best is None:
        raise RuntimeError("No ElasticNet candidate fitted")
    return best[1], best[2]


def rolling_predictions(weekly: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    features = [f"rank_{column}" for column in indicator_columns()]
    weeks = sorted(weekly.week.unique())
    predictions: list[pd.DataFrame] = []
    coefficients: list[dict[str, Any]] = []
    for week in weeks:
        history_start = week - pd.Timedelta(weeks=TRAINING_WEEKS)
        training = weekly.loc[
            weekly.week.ge(history_start) & weekly.week.lt(week)
        ]
        current = weekly.loc[weekly.week.eq(week)].copy()
        if len(training) < MINIMUM_TRAINING_ROWS or current.empty:
            continue
        model, alpha = fit_positive_elastic_net(training, features)
        current["score"] = model.predict(current[features].to_numpy(dtype="float64"))
        current["score_rank"] = current.score.rank(pct=True, method="average")
        predictions.append(current)
        coefficient = {
            "week": str(week),
            "alpha": alpha,
            "selected_features": int(np.count_nonzero(model.coef_)),
        }
        coefficient.update(
            {
                feature: float(value)
                for feature, value in zip(features, model.coef_)
                if value > 0
            }
        )
        coefficients.append(coefficient)
    if not predictions:
        return pd.DataFrame(), pd.DataFrame(coefficients)
    return pd.concat(predictions, ignore_index=True), pd.DataFrame(coefficients)


def _position_matrix(
    selected: pd.DataFrame,
    all_weeks: pd.Index,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    positions = selected.pivot(index="week", columns="symbol", values="position")
    positions = positions.reindex(all_weeks, fill_value=0.0).fillna(0.0)
    returns = selected.pivot(index="week", columns="symbol", values="next_return")
    returns = returns.reindex(index=all_weeks, columns=positions.columns).fillna(0.0)
    return positions, returns


def portfolio_returns(
    predictions: pd.DataFrame,
    one_way_cost: float,
    variant: str,
) -> pd.DataFrame:
    if predictions.empty:
        return pd.DataFrame()
    frame = predictions.copy()
    frame["position"] = 0.0
    if variant == "quintile":
        long_mask = frame.score_rank.ge(0.8)
        short_mask = frame.score_rank.le(0.2)
        for _, group in frame.groupby("week", sort=True):
            for mask, sign in ((long_mask, 1.0), (short_mask, -1.0)):
                side = group.index.intersection(frame.index[mask])
                if side.empty:
                    continue
                weights = np.sqrt(frame.loc[side, "median_daily_volume_30d"])
                frame.loc[side, "position"] = sign * 0.5 * weights / weights.sum()
    elif variant in {"single", "top3"}:
        count = 1 if variant == "single" else 3
        selected_parts: list[pd.DataFrame] = []
        for _, group in frame.groupby("week", sort=True):
            ranked = group.assign(
                distance=(group.score_rank - 0.5).abs(),
                direction=np.where(group.score_rank.ge(0.5), 1.0, -1.0),
            ).nlargest(count, ["distance", "median_daily_volume_30d"])
            selected_parts.append(ranked)
        chosen = pd.concat(selected_parts)
        frame.loc[chosen.index, "position"] = chosen.direction / chosen.groupby(
            "week"
        ).direction.transform("count")
    else:
        raise ValueError(f"Unknown variant: {variant}")
    selected = frame.loc[frame.position.ne(0)].copy()
    all_weeks = pd.Index(sorted(frame.week.unique()), name="week")
    positions, returns = _position_matrix(selected, all_weeks)
    turnover = positions.diff().abs().sum(axis=1)
    if len(turnover):
        turnover.iloc[0] = positions.iloc[0].abs().sum()
    gross = (positions * returns).sum(axis=1)
    return pd.DataFrame(
        {
            "week": all_weeks,
            "gross_return": gross.values,
            "turnover": turnover.values,
            "net_return": (gross - turnover * one_way_cost).values,
            "positions": positions.ne(0).sum(axis=1).values,
        }
    )


def weekly_metrics(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {"weeks": 0, "total_return_pct": 0.0, "profit_factor": 0.0}
    returns = frame.net_return.fillna(0.0)
    equity = (1.0 + returns).cumprod()
    drawdown = equity / equity.cummax() - 1.0
    volatility = float(returns.std())
    metrics = {
        "days": int(len(frame)),
        "total_return_pct": round(float((equity.iloc[-1] - 1.0) * 100), 6),
        "annualized_return_pct": round(
            float((equity.iloc[-1] ** (52 / len(frame)) - 1.0) * 100), 6
        ),
        "annualized_volatility_pct": round(volatility * math.sqrt(52) * 100, 6),
        "sharpe": round(float(returns.mean() / volatility * math.sqrt(52)), 6)
        if volatility > 0
        else 0.0,
        "max_drawdown_pct": round(float(drawdown.min() * 100), 6),
        "turnover": round(float(frame.turnover.sum()), 6),
    }
    winners = frame.net_return.clip(lower=0).sum()
    losers = -frame.net_return.clip(upper=0).sum()
    metrics.update(
        {
            "weeks": int(len(frame)),
            "win_rate_pct": round(float(frame.net_return.gt(0).mean() * 100), 6),
            "profit_factor": round(float(winners / losers), 6)
            if losers
            else (999.0 if winners else 0.0),
            "cost_sum_pct": round(
                float((frame.gross_return - frame.net_return).sum() * 100), 6
            ),
        }
    )
    return metrics


def evaluate(frame: pd.DataFrame) -> dict[str, Any]:
    windows = {
        "development_2025h1": ("2025-01-01", "2025-07-01"),
        "validation_2025h2": ("2025-07-01", "2026-01-01"),
        "blind_2026h1": ("2026-01-01", "2026-07-01"),
    }
    return {
        name: weekly_metrics(frame.loc[frame.week.ge(start) & frame.week.lt(end)])
        for name, (start, end) in windows.items()
    }


def candidate_passes(report: dict[str, Any]) -> bool:
    return all(
        item.get("weeks", 0) >= 20
        and item.get("total_return_pct", 0.0) > 0
        and item.get("profit_factor", 0.0) > 1.0
        for item in report.values()
    )


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    daily, source = load_daily_panel([args.data, args.audit_data])
    weekly = weekly_panel(daily)
    predictions, coefficients = rolling_predictions(weekly)
    results: dict[str, Any] = {}
    for variant in ("quintile", "single", "top3"):
        for cost_name, cost in (
            ("base", ONE_WAY_COST),
            ("stress", ONE_WAY_STRESS_COST),
        ):
            name = f"{variant}_{cost_name}"
            returns = portfolio_returns(predictions, cost, variant)
            report = evaluate(returns)
            results[name] = {
                "one_way_cost": cost,
                "windows": report,
                "passes": candidate_passes(report),
            }
            returns.to_parquet(args.output / f"{name}.parquet", index=False)
    coefficients.to_parquet(args.output / "coefficients.parquet", index=False)
    accepted_variants = [
        variant
        for variant in ("quintile", "single", "top3")
        if results[f"{variant}_base"]["passes"]
        and results[f"{variant}_stress"]["passes"]
    ]
    payload = {
        "method": "CTREND-inspired rolling positive ElasticNet",
        "adaptations": [
            "Binance USDT perpetual point-in-time universe",
            "sqrt trailing quote volume replaces unavailable market cap weights",
            "pooled rolling positive ElasticNet with AICc alpha selection",
        ],
        "features": indicator_columns(),
        "training_weeks": TRAINING_WEEKS,
        "source": {
            "datasets": len(source["manifests"]),
            "sources": [item.get("source") for item in source["manifests"]],
            "checksum_verified": all(
                bool(item.get("checksum_verified")) for item in source["manifests"]
            ),
        },
        "prediction_rows": int(len(predictions)),
        "prediction_weeks": int(predictions.week.nunique()) if len(predictions) else 0,
        "results": results,
        "accepted_variants": accepted_variants,
        "accepted": bool(accepted_variants),
    }
    (args.output / "report.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
