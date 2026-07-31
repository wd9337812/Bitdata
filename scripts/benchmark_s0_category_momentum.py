from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_s0_donchian_ensemble import ONE_WAY_COST  # noqa: E402
from scripts.benchmark_s0_volume_weighted_tsmom import (  # noqa: E402
    STABLE_BASES,
    base_asset,
)
from scripts.benchmark_s0_xmom_point_in_time import load_manifest  # noqa: E402


DEFAULT_DATA = (
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2024_2025"
)
DEFAULT_AUDIT_DATA = ROOT / "data" / "research" / "binance_um_point_in_time_1h"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_category_momentum"
CLUSTER_LOOKBACK_DAYS = 90
FORMATION_DAYS = 7
MINIMUM_HISTORY_DAYS = 60
MINIMUM_AGE_DAYS = 90
MINIMUM_MEDIAN_VOLUME = 1_000_000.0
MAXIMUM_UNIVERSE = 150
TARGET_CLUSTERS = 20
SELECTED_CATEGORIES_PER_SIDE = 5
STRESS_ONE_WAY_COST = 0.0012


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Point-in-time correlation-cluster adaptation of published "
            "cryptocurrency category momentum."
        )
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--audit-data", type=Path, default=DEFAULT_AUDIT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def daily_symbol_market(paths: Path | Iterable[Path], start_ms: int) -> pd.DataFrame:
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
    hourly = (
        pd.concat(
            [pd.read_parquet(path, columns=columns) for path in paths],
            ignore_index=True,
        )
        .sort_values("open_time")
        .drop_duplicates("open_time", keep="last")
    )
    hourly["day"] = pd.to_datetime(hourly.open_time, unit="ms", utc=True).dt.floor(
        "D"
    )
    frame = (
        hourly.groupby(["symbol", "day"], as_index=False, sort=True)
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
    frame["return"] = frame.close.pct_change()
    frame["next_return"] = frame.close.shift(-1) / frame.close - 1.0
    next_day = frame.day.shift(-1)
    frame.loc[
        ~next_day.sub(frame.day).eq(pd.Timedelta(days=1)), "next_return"
    ] = np.nan
    frame["formation_return"] = frame.close.pct_change(FORMATION_DAYS)
    frame.loc[
        ~frame.day.sub(frame.day.shift(FORMATION_DAYS)).eq(
            pd.Timedelta(days=FORMATION_DAYS)
        ),
        "formation_return",
    ] = np.nan
    frame["median_volume_30d"] = frame.quote_volume.rolling(
        30, min_periods=20
    ).median()
    listed_at = pd.to_datetime(int(start_ms), unit="ms", utc=True)
    frame["symbol_age_days"] = (
        frame.day - listed_at
    ).dt.total_seconds() / 86_400.0
    return frame


def load_daily_panel(paths: Iterable[Path]) -> tuple[pd.DataFrame, dict[str, Any]]:
    per_symbol: dict[str, list[Path]] = {}
    starts: dict[str, int] = {}
    manifests: list[dict[str, Any]] = []
    for data in paths:
        if not data.exists():
            continue
        source_starts, manifest = load_manifest(data)
        manifests.append(manifest)
        for symbol, start in source_starts.items():
            starts[symbol] = min(starts.get(symbol, start), start)
        for path in sorted((data / "parquet").glob("*.parquet")):
            symbol = path.stem
            if symbol not in source_starts or base_asset(symbol) in STABLE_BASES:
                continue
            per_symbol.setdefault(symbol, []).append(path)
    frames = [
        daily_symbol_market(symbol_paths, starts[symbol])
        for symbol, symbol_paths in per_symbol.items()
    ]
    if not frames:
        raise ValueError("No point-in-time source files found")
    panel = pd.concat(frames, ignore_index=True).sort_values(["day", "symbol"])
    source = {
        "datasets": len(manifests),
        "sources": [manifest.get("source") for manifest in manifests],
        "checksum_verified": all(
            bool(manifest.get("checksum_verified")) for manifest in manifests
        ),
    }
    return panel.reset_index(drop=True), source


def month_boundaries(panel: pd.DataFrame) -> pd.DatetimeIndex:
    start = (panel.day.min() + pd.offsets.MonthBegin(1)).normalize()
    end = panel.day.max().normalize()
    return pd.date_range(start, end, freq="MS", tz="UTC")


def cluster_one_month(panel: pd.DataFrame, boundary: pd.Timestamp) -> pd.DataFrame:
    start = boundary - pd.Timedelta(days=CLUSTER_LOOKBACK_DAYS)
    history = panel.loc[panel.day.ge(start) & panel.day.lt(boundary)].copy()
    latest = history.groupby("symbol", sort=False).tail(1)
    universe = latest.loc[
        latest.symbol_age_days.ge(MINIMUM_AGE_DAYS)
        & latest.median_volume_30d.ge(MINIMUM_MEDIAN_VOLUME)
    ].nlargest(MAXIMUM_UNIVERSE, "median_volume_30d")
    if len(universe) < 20:
        return pd.DataFrame()
    symbols = universe.symbol.astype(str).tolist()
    returns = history.loc[history.symbol.isin(symbols)].pivot(
        index="day", columns="symbol", values="return"
    )
    valid = returns.notna().sum().ge(MINIMUM_HISTORY_DAYS)
    returns = returns.loc[:, valid]
    if returns.shape[1] < 20:
        return pd.DataFrame()
    standardized = (returns - returns.mean()) / returns.std().replace(0.0, np.nan)
    matrix = standardized.fillna(0.0).T.to_numpy(dtype="float64")
    components = min(10, matrix.shape[0] - 1, matrix.shape[1])
    embedding = PCA(n_components=components, random_state=42).fit_transform(matrix)
    clusters = min(TARGET_CLUSTERS, max(4, len(returns.columns) // 5))
    labels = KMeans(
        n_clusters=clusters,
        n_init=20,
        random_state=42,
    ).fit_predict(embedding)
    return pd.DataFrame(
        {
            "month": boundary.strftime("%Y-%m"),
            "symbol": returns.columns.astype(str),
            "cluster": labels.astype(int),
        }
    )


def monthly_clusters(panel: pd.DataFrame) -> pd.DataFrame:
    parts = [cluster_one_month(panel, boundary) for boundary in month_boundaries(panel)]
    parts = [part for part in parts if not part.empty]
    if not parts:
        return pd.DataFrame(columns=["month", "symbol", "cluster"])
    return pd.concat(parts, ignore_index=True)


def attach_clusters(panel: pd.DataFrame, clusters: pd.DataFrame) -> pd.DataFrame:
    frame = panel.copy()
    frame["month"] = frame.day.dt.strftime("%Y-%m")
    return frame.merge(clusters, on=["month", "symbol"], how="inner")


def category_signals(selected: pd.DataFrame) -> pd.DataFrame:
    frame = selected.dropna(
        subset=["formation_return", "next_return", "median_volume_30d"]
    ).copy()
    frame["signal_weight"] = np.sqrt(frame.median_volume_30d.clip(lower=0.0))
    frame["weighted_signal"] = frame.formation_return * frame.signal_weight
    grouped = frame.groupby(["day", "cluster"], sort=True)
    signal = grouped.weighted_signal.sum() / grouped.signal_weight.sum()
    result = signal.rename("category_signal").reset_index()
    result["category_rank"] = result.groupby("day").category_signal.rank(
        pct=True, method="average"
    )
    return frame.merge(result, on=["day", "cluster"], how="inner")


def choose_positions(signals: pd.DataFrame, variant: str) -> pd.DataFrame:
    frame = signals.copy()
    frame["position"] = 0.0
    if variant == "category_portfolio":
        category_table = frame[
            ["day", "cluster", "category_signal", "category_rank"]
        ].drop_duplicates(["day", "cluster"])
        category_table["side"] = 0.0
        for _, group in category_table.groupby("day", sort=True):
            longs = group.nlargest(SELECTED_CATEGORIES_PER_SIDE, "category_signal")
            shorts = group.nsmallest(SELECTED_CATEGORIES_PER_SIDE, "category_signal")
            category_table.loc[longs.index, "side"] = 1.0
            category_table.loc[shorts.index, "side"] = -1.0
        frame = frame.merge(
            category_table[["day", "cluster", "side"]],
            on=["day", "cluster"],
            how="left",
        )
        active = frame.side.ne(0)
        within_weight = np.sqrt(frame.loc[active, "median_volume_30d"])
        denominator = within_weight.groupby(
            [frame.loc[active, "day"], frame.loc[active, "cluster"]]
        ).transform("sum")
        per_category = 0.5 / SELECTED_CATEGORIES_PER_SIDE
        frame.loc[active, "position"] = (
            frame.loc[active, "side"] * per_category * within_weight / denominator
        )
    elif variant in {"single", "top3"}:
        count = 1 if variant == "single" else 3
        category_table = frame[
            ["day", "cluster", "category_signal"]
        ].drop_duplicates(["day", "cluster"])
        category_table["strength"] = category_table.category_signal.abs()
        chosen_categories = (
            category_table.sort_values(
                ["day", "strength"], ascending=[True, False]
            )
            .groupby("day", sort=False)
            .head(count)
        )
        representatives = frame.merge(
            chosen_categories[["day", "cluster"]],
            on=["day", "cluster"],
            how="inner",
        )
        representatives = (
            representatives.sort_values(
                ["day", "cluster", "median_volume_30d"],
                ascending=[True, True, False],
            )
            .groupby(["day", "cluster"], sort=False)
            .head(1)
        )
        representatives["side"] = np.sign(representatives.category_signal)
        representatives["position"] = representatives.side / representatives.groupby(
            "day"
        ).side.transform("count")
        return representatives.loc[representatives.position.ne(0)].copy()
    else:
        raise ValueError(f"Unknown variant: {variant}")
    return frame.loc[frame.position.ne(0)].copy()


def strategy_returns(
    signals: pd.DataFrame,
    variant: str,
    one_way_cost: float,
) -> pd.DataFrame:
    selected = choose_positions(signals, variant)
    all_days = pd.Index(sorted(signals.day.unique()), name="day")
    positions = selected.pivot(index="day", columns="symbol", values="position")
    positions = positions.reindex(all_days, fill_value=0.0).fillna(0.0)
    returns = selected.pivot(index="day", columns="symbol", values="next_return")
    returns = returns.reindex(index=all_days, columns=positions.columns).fillna(0.0)
    turnover = positions.diff().abs().sum(axis=1)
    if len(turnover):
        turnover.iloc[0] = positions.iloc[0].abs().sum()
    gross = (positions * returns).sum(axis=1)
    return pd.DataFrame(
        {
            "day": all_days,
            "gross_return": gross.values,
            "turnover": turnover.values,
            "net_return": (gross - turnover * one_way_cost).values,
            "positions": positions.ne(0).sum(axis=1).values,
        }
    )


def daily_metrics(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {"days": 0, "total_return_pct": 0.0, "profit_factor": 0.0}
    returns = frame.net_return.fillna(0.0)
    equity = (1.0 + returns).cumprod()
    drawdown = equity / equity.cummax() - 1.0
    volatility = float(returns.std())
    winners = returns.clip(lower=0).sum()
    losers = -returns.clip(upper=0).sum()
    return {
        "days": int(len(frame)),
        "total_return_pct": round(float((equity.iloc[-1] - 1.0) * 100), 6),
        "annualized_return_pct": round(
            float((equity.iloc[-1] ** (365 / len(frame)) - 1.0) * 100), 6
        ),
        "annualized_volatility_pct": round(volatility * math.sqrt(365) * 100, 6),
        "sharpe": round(float(returns.mean() / volatility * math.sqrt(365)), 6)
        if volatility > 0
        else 0.0,
        "max_drawdown_pct": round(float(drawdown.min() * 100), 6),
        "turnover": round(float(frame.turnover.sum()), 6),
        "win_rate_pct": round(float(returns.gt(0).mean() * 100), 6),
        "profit_factor": round(float(winners / losers), 6)
        if losers
        else (999.0 if winners else 0.0),
        "cost_sum_pct": round(
            float((frame.gross_return - frame.net_return).sum() * 100), 6
        ),
    }


def evaluate(frame: pd.DataFrame) -> dict[str, Any]:
    windows = {
        "development_2024h2": ("2024-07-01", "2025-01-01"),
        "validation_2025h1": ("2025-01-01", "2025-07-01"),
        "test_2025h2": ("2025-07-01", "2026-01-01"),
        "blind_2026h1": ("2026-01-01", "2026-07-01"),
    }
    return {
        name: daily_metrics(frame.loc[frame.day.ge(start) & frame.day.lt(end)])
        for name, (start, end) in windows.items()
    }


def candidate_passes(report: dict[str, Any]) -> bool:
    return all(
        item.get("days", 0) >= 150
        and item.get("total_return_pct", 0.0) > 0
        and item.get("profit_factor", 0.0) > 1.0
        for item in report.values()
    )


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    panel, source = load_daily_panel([args.data, args.audit_data])
    clusters = monthly_clusters(panel)
    signals = category_signals(attach_clusters(panel, clusters))
    results: dict[str, Any] = {}
    for variant in ("category_portfolio", "single", "top3"):
        for cost_name, cost in (
            ("base", ONE_WAY_COST),
            ("stress", STRESS_ONE_WAY_COST),
        ):
            name = f"{variant}_{cost_name}"
            returns = strategy_returns(signals, variant, cost)
            report = evaluate(returns)
            results[name] = {
                "one_way_cost": cost,
                "windows": report,
                "passes": candidate_passes(report),
            }
            returns.to_parquet(args.output / f"{name}.parquet", index=False)
    accepted_variants = [
        variant
        for variant in ("category_portfolio", "single", "top3")
        if results[f"{variant}_base"]["passes"]
        and results[f"{variant}_stress"]["passes"]
    ]
    payload = {
        "method": "point-in-time correlation-cluster category momentum",
        "formation_days": FORMATION_DAYS,
        "holding_days": 1,
        "cluster_lookback_days": CLUSTER_LOOKBACK_DAYS,
        "target_clusters": TARGET_CLUSTERS,
        "source": source,
        "cluster_months": int(clusters.month.nunique()),
        "clustered_symbols": int(clusters.symbol.nunique()),
        "results": results,
        "accepted_variants": accepted_variants,
        "accepted": bool(accepted_variants),
    }
    clusters.to_parquet(args.output / "monthly_clusters.parquet", index=False)
    (args.output / "report.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
