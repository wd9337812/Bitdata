from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_s0_volume_weighted_tsmom import (  # noqa: E402
    build_daily_panel,
)


DEFAULT_DATA = (
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2020_2023",
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2024_2025",
    ROOT / "data" / "research" / "binance_um_point_in_time_1h",
)
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_dispersion_scaled_momentum"
MOMENTUM_DAYS = 20
MINIMUM_AGE_DAYS = 90
MINIMUM_MEDIAN_DAILY_VOLUME = 25_000_000.0
MINIMUM_UNIVERSE = 3
MINIMUM_EXPOSURE = 0.10
SMOOTHING = 0.80
COSTS = {
    "paper_0_005pct_one_way": 0.00005,
    "base_0_06pct_one_way": 0.0006,
    "stress_0_12pct_one_way": 0.0012,
}
WINDOWS = {
    "development_2021_2023": ("2021-01-01", "2024-01-01"),
    "validation_2024": ("2024-01-01", "2025-01-01"),
    "test_2025_2026": ("2025-01-01", "2027-01-01"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Point-in-time audit of dispersion-scaled 20-day cryptocurrency "
            "cross-sectional momentum."
        )
    )
    parser.add_argument("--data", type=Path, action="append")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def combine_point_in_time_panels(data_dirs: Iterable[Path]) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for data in data_dirs:
        panel, _ = build_daily_panel(data)
        parts.append(panel)
    if not parts:
        raise ValueError("No point-in-time data directories supplied")
    combined = pd.concat(parts, ignore_index=True)
    combined = combined.sort_values(["symbol", "day"]).drop_duplicates(
        ["symbol", "day"], keep="last"
    )
    return prepare_panel(combined)


def prepare_panel(panel: pd.DataFrame) -> pd.DataFrame:
    required = {
        "symbol",
        "day",
        "close",
        "quote_volume",
        "symbol_age_days",
    }
    missing = required - set(panel.columns)
    if missing:
        raise ValueError(f"Panel missing columns: {sorted(missing)}")
    result = panel.loc[:, sorted(required)].copy()
    result["day"] = pd.to_datetime(result.day, utc=True)
    result = result.sort_values(["symbol", "day"]).reset_index(drop=True)
    grouped = result.groupby("symbol", sort=False)
    previous_day = grouped.day.shift(1)
    previous_close = grouped.close.shift(1)
    lag_day = grouped.day.shift(MOMENTUM_DAYS)
    lag_close = grouped.close.shift(MOMENTUM_DAYS)
    next_day = grouped.day.shift(-1)
    next_close = grouped.close.shift(-1)
    result["return_1d"] = result.close / previous_close - 1.0
    result.loc[
        ~result.day.sub(previous_day).eq(pd.Timedelta(days=1)), "return_1d"
    ] = np.nan
    result["momentum_20d"] = np.log(result.close / lag_close)
    result.loc[
        ~result.day.sub(lag_day).eq(pd.Timedelta(days=MOMENTUM_DAYS)),
        "momentum_20d",
    ] = np.nan
    result["next_return"] = next_close / result.close - 1.0
    result.loc[
        ~next_day.sub(result.day).eq(pd.Timedelta(days=1)), "next_return"
    ] = np.nan
    result["median_quote_volume_30d"] = grouped.quote_volume.transform(
        lambda values: values.rolling(30, min_periods=20).median()
    )
    return result


def eligible_cross_section(panel: pd.DataFrame) -> pd.DataFrame:
    frame = panel.loc[
        panel.symbol_age_days.ge(MINIMUM_AGE_DAYS)
        & panel.median_quote_volume_30d.ge(MINIMUM_MEDIAN_DAILY_VOLUME)
        & panel.return_1d.notna()
        & panel.momentum_20d.notna()
        & panel.next_return.notna()
    ].copy()
    frame["universe_size"] = frame.groupby("day", sort=False).symbol.transform(
        "size"
    )
    frame = frame.loc[frame.universe_size.ge(MINIMUM_UNIVERSE)].copy()
    frame["mean_momentum"] = frame.groupby("day", sort=False)[
        "momentum_20d"
    ].transform("mean")
    frame["demeaned_signal"] = frame.momentum_20d - frame.mean_momentum
    frame["gross_denominator"] = frame.groupby("day", sort=False)[
        "demeaned_signal"
    ].transform(lambda values: values.abs().sum())
    frame = frame.loc[frame.gross_denominator.gt(0)].copy()
    frame["base_weight"] = frame.demeaned_signal / frame.gross_denominator
    return frame


def dispersion_exposure(frame: pd.DataFrame) -> pd.DataFrame:
    daily = (
        frame.groupby("day", sort=True)
        .agg(
            dispersion=("return_1d", lambda values: float(np.std(values, ddof=0))),
            universe_size=("symbol", "size"),
        )
        .reset_index()
    )
    daily["dispersion_target"] = (
        daily.dispersion.expanding(min_periods=MOMENTUM_DAYS).median().shift(1)
    )
    raw = (daily.dispersion_target / daily.dispersion.replace(0, np.nan)).clip(
        lower=MINIMUM_EXPOSURE,
        upper=1.0,
    )
    raw = raw.fillna(1.0)
    smoothed: list[float] = []
    previous = 1.0
    for value in raw:
        previous = SMOOTHING * previous + (1.0 - SMOOTHING) * float(value)
        smoothed.append(previous)
    daily["raw_exposure"] = raw
    daily["smoothed_exposure"] = smoothed
    return daily


def btc_volatility_exposure(panel: pd.DataFrame) -> pd.DataFrame:
    btc = panel.loc[panel.symbol.eq("BTCUSDT"), ["day", "return_1d"]].copy()
    btc = btc.sort_values("day").drop_duplicates("day", keep="last")
    btc["dispersion"] = btc.return_1d.rolling(
        30, min_periods=20
    ).std(ddof=0)
    btc["dispersion_target"] = (
        btc.dispersion.expanding(min_periods=MOMENTUM_DAYS).median().shift(1)
    )
    raw = (btc.dispersion_target / btc.dispersion.replace(0, np.nan)).clip(
        lower=MINIMUM_EXPOSURE,
        upper=1.0,
    )
    raw = raw.fillna(1.0)
    smoothed: list[float] = []
    previous = 1.0
    for value in raw:
        previous = SMOOTHING * previous + (1.0 - SMOOTHING) * float(value)
        smoothed.append(previous)
    return pd.DataFrame(
        {
            "day": btc.day,
            "btc_volatility_30d": btc.dispersion,
            "volatility_target": btc.dispersion_target,
            "raw_exposure": raw,
            "smoothed_exposure": smoothed,
        }
    )


def position_matrix(
    frame: pd.DataFrame,
    exposure: pd.DataFrame,
    implementation: str,
    scaled: bool,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    selected = frame.copy()
    if implementation in {"single_position", "single_long_only"}:
        if implementation == "single_long_only":
            selected = selected.loc[selected.demeaned_signal.gt(0)].copy()
        selected = (
            selected.assign(abs_signal=selected.demeaned_signal.abs())
            .sort_values(
                ["day", "abs_signal", "median_quote_volume_30d", "symbol"],
                ascending=[True, False, False, True],
            )
            .groupby("day", sort=False)
            .head(1)
            .copy()
        )
        selected["base_weight"] = (
            1.0
            if implementation == "single_long_only"
            else np.sign(selected.demeaned_signal)
        )
    elif implementation == "long_only_portfolio":
        selected = selected.loc[selected.demeaned_signal.gt(0)].copy()
        positive_sum = selected.groupby("day", sort=False)[
            "demeaned_signal"
        ].transform("sum")
        selected["base_weight"] = selected.demeaned_signal / positive_sum
    elif implementation != "paper_portfolio":
        raise ValueError(f"Unknown implementation: {implementation}")
    if scaled:
        exposure_map = exposure.set_index("day").smoothed_exposure
        selected["position"] = (
            selected.base_weight * selected.day.map(exposure_map).fillna(1.0)
        )
    else:
        selected["position"] = selected.base_weight
    days = pd.Index(sorted(frame.day.unique()), name="day")
    positions = (
        selected.pivot(index="day", columns="symbol", values="position")
        .reindex(days, fill_value=0.0)
        .fillna(0.0)
    )
    forward = (
        selected.pivot(index="day", columns="symbol", values="next_return")
        .reindex(index=days, columns=positions.columns)
        .fillna(0.0)
    )
    return positions, forward


def simulate(
    frame: pd.DataFrame,
    exposure: pd.DataFrame,
    implementation: str,
    scaled: bool,
    one_way_cost: float,
) -> pd.DataFrame:
    positions, forward = position_matrix(frame, exposure, implementation, scaled)
    turnover = positions.diff().abs().sum(axis=1)
    if len(turnover):
        turnover.iloc[0] = positions.iloc[0].abs().sum()
    gross = (positions * forward).sum(axis=1)
    return pd.DataFrame(
        {
            "day": positions.index,
            "gross_return": gross.values,
            "turnover": turnover.values,
            "cost_return": (turnover * one_way_cost).values,
            "net_return": (gross - turnover * one_way_cost).values,
            "gross_exposure": positions.abs().sum(axis=1).values,
            "long_count": positions.gt(0).sum(axis=1).values,
            "short_count": positions.lt(0).sum(axis=1).values,
        }
    )


def metrics(daily: pd.DataFrame) -> dict[str, float | int]:
    if daily.empty:
        return {
            "days": 0,
            "total_return_pct": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_pct": 0.0,
        }
    returns = daily.net_return.fillna(0.0).clip(lower=-0.999999)
    equity = (1.0 + returns).cumprod()
    drawdown = equity / equity.cummax() - 1.0
    gains = float(returns.clip(lower=0).sum())
    losses = float(-returns.clip(upper=0).sum())
    volatility = float(returns.std())
    gross_profit = float(daily.gross_return.clip(lower=0).sum())
    return {
        "days": int(len(daily)),
        "active_days": int(daily.gross_exposure.gt(0).sum()),
        "total_return_pct": round(float((equity.iloc[-1] - 1.0) * 100.0), 6),
        "net_return_sum_pct": round(float(returns.sum() * 100.0), 6),
        "profit_factor": round(gains / losses, 6) if losses else (999.0 if gains else 0.0),
        "annualized_sharpe": round(
            float(returns.mean() / volatility * math.sqrt(365)), 6
        )
        if volatility > 0
        else 0.0,
        "max_drawdown_pct": round(float(drawdown.min() * 100.0), 6),
        "turnover": round(float(daily.turnover.sum()), 6),
        "cost_pct_points": round(float(daily.cost_return.sum() * 100.0), 6),
        "cost_to_positive_gross_pct": round(
            float(daily.cost_return.sum() / gross_profit * 100.0), 6
        )
        if gross_profit > 0
        else 0.0,
        "average_gross_exposure": round(float(daily.gross_exposure.mean()), 6),
    }


def window_metrics(daily: pd.DataFrame) -> dict[str, dict[str, float | int]]:
    result: dict[str, dict[str, float | int]] = {}
    for name, (start, end) in WINDOWS.items():
        mask = daily.day.ge(start) & daily.day.lt(end)
        result[name] = metrics(daily.loc[mask])
    years = sorted(daily.day.dt.year.unique())
    result["yearly"] = {
        str(year): metrics(daily.loc[daily.day.dt.year.eq(year)]) for year in years
    }
    result["all"] = metrics(daily)
    return result


def qualifies(report: dict[str, Any]) -> bool:
    candidates = (
        ("single_position", "scaled"),
        ("single_long_only", "btc_vol_scaled"),
    )
    for implementation, scale in candidates:
        stress = report["implementations"][implementation][scale][
            "stress_0_12pct_one_way"
        ]
        baseline = report["implementations"][implementation]["unscaled"][
            "stress_0_12pct_one_way"
        ]
        passed = True
        for window in WINDOWS:
            item = stress[window]
            passed &= (
                item["total_return_pct"] > 0
                and item["profit_factor"] > 1.0
                and item["max_drawdown_pct"]
                >= baseline[window]["max_drawdown_pct"]
            )
        if passed:
            return True
    return False


def run(data_dirs: Iterable[Path]) -> tuple[dict[str, Any], dict[str, pd.DataFrame]]:
    panel = combine_point_in_time_panels(data_dirs)
    eligible = eligible_cross_section(panel)
    exposure = dispersion_exposure(eligible)
    btc_exposure = btc_volatility_exposure(panel)
    reports: dict[str, Any] = {}
    outputs: dict[str, pd.DataFrame] = {
        "dispersion_exposure": exposure,
        "btc_volatility_exposure": btc_exposure,
    }
    for implementation in ("paper_portfolio", "single_position"):
        reports[implementation] = {}
        for scaled in (False, True):
            scale_name = "scaled" if scaled else "unscaled"
            reports[implementation][scale_name] = {}
            for cost_name, cost in COSTS.items():
                daily = simulate(
                    eligible,
                    exposure,
                    implementation,
                    scaled,
                    cost,
                )
                reports[implementation][scale_name][cost_name] = window_metrics(daily)
                outputs[f"{implementation}_{scale_name}_{cost_name}"] = daily
    for implementation in ("long_only_portfolio", "single_long_only"):
        reports[implementation] = {}
        for scale_name, scale_frame, scaled in (
            ("unscaled", btc_exposure, False),
            ("btc_vol_scaled", btc_exposure, True),
        ):
            reports[implementation][scale_name] = {}
            for cost_name, cost in COSTS.items():
                daily = simulate(
                    eligible,
                    scale_frame,
                    implementation,
                    scaled,
                    cost,
                )
                reports[implementation][scale_name][cost_name] = window_metrics(daily)
                outputs[f"{implementation}_{scale_name}_{cost_name}"] = daily
    report: dict[str, Any] = {
        "experiment": "s0_dispersion_scaled_cross_sectional_momentum",
        "status": "research_only",
        "pre_registered_method": {
            "momentum_days": MOMENTUM_DAYS,
            "minimum_age_days": MINIMUM_AGE_DAYS,
            "minimum_median_daily_quote_volume": MINIMUM_MEDIAN_DAILY_VOLUME,
            "minimum_universe": MINIMUM_UNIVERSE,
            "dispersion": "cross-sectional population std of lagged 1d returns",
            "target": "expanding median dispersion through t-1",
            "exposure": "clip(target/current, 0.10, 1.00)",
            "smoothing": SMOOTHING,
            "signal": "20d cumulative log return, demeaned, unit gross",
            "long_only_adaptation": (
                "positive demeaned momentum weights; separate single-winner "
                "adaptation; BTC 30d realized-volatility scaling"
            ),
            "execution": "close(t) signal earns close(t) to close(t+1)",
            "costs": COSTS,
            "windows": WINDOWS,
        },
        "limitations": [
            "Binance USD-M quote volume is used because historical point-in-time market capitalization is unavailable.",
            "Daily close execution is optimistic relative to intraday path execution; passing this audit would still require minute-level replay.",
            "The paper portfolio is not executable for a very small S0 account, so a separate single-position adaptation is mandatory.",
        ],
        "coverage": {
            "rows": int(len(panel)),
            "eligible_rows": int(len(eligible)),
            "symbols": int(panel.symbol.nunique()),
            "eligible_symbols": int(eligible.symbol.nunique()),
            "start": panel.day.min().isoformat(),
            "end": panel.day.max().isoformat(),
            "median_daily_universe": float(
                eligible.groupby("day").symbol.nunique().median()
            ),
        },
        "implementations": reports,
    }
    report["qualified_for_minute_replay"] = qualifies(report)
    report["decision"] = (
        "continue_to_minute_replay"
        if report["qualified_for_minute_replay"]
        else "reject_before_shadow_or_live"
    )
    return report, outputs


def main() -> None:
    args = parse_args()
    data_dirs = tuple(args.data) if args.data else DEFAULT_DATA
    args.output.mkdir(parents=True, exist_ok=True)
    report, outputs = run(data_dirs)
    for name, frame in outputs.items():
        frame.to_parquet(args.output / f"{name}.parquet", index=False, compression="zstd")
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
