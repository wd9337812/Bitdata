from __future__ import annotations

import argparse
import gc
import heapq
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_s0_xmom_point_in_time import (  # noqa: E402
    build_panel,
    load_manifest,
)

DATA_DIRS = (
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2020_2023",
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2024_2025",
    ROOT / "data" / "research" / "binance_um_point_in_time_1h",
)
FUNDING_DIRS = (
    ROOT / "data" / "research" / "binance_um_point_in_time_funding_2020_2023",
    ROOT / "data" / "research" / "binance_um_point_in_time_funding_2024_2025",
    ROOT / "data" / "research" / "binance_um_point_in_time_funding",
)
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_funding_crowding_reversal"
BASE_ONE_WAY_COST = 0.0012
STRESS_ONE_WAY_COST = 0.0024
WINDOWS = {
    "development_2020_2022": ("2020-01-01", "2023-01-01"),
    "validation_2023": ("2023-01-01", "2024-01-01"),
    "test_2024": ("2024-01-01", "2025-01-01"),
    "blind_2025": ("2025-01-01", "2026-01-01"),
    "final_2026": ("2026-01-01", "2027-01-01"),
}


@dataclass(frozen=True)
class Profile:
    name: str
    min_abs_funding_pct: float
    hold_hours: int
    stop_pct: float | None
    take_r: float | None
    min_liquidity_24h: float = 20_000_000
    min_age_days: int = 45


# Frozen before evaluation.
PROFILES = tuple(
    Profile(
        name=f"fund_rev_{thr:g}_hold{hold}h_stop{stop:g}{'_tp'+str(take_r) if take_r else ''}",
        min_abs_funding_pct=thr,
        hold_hours=hold,
        stop_pct=stop,
        take_r=take_r,
    )
    for thr in (0.10, 0.15)
    for hold in (24, 72)
    for stop in (0.10,)
    for take_r in (None, 2.0)
)


def load_funding(dirs: tuple[Path, ...]) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for directory in dirs:
        if not directory.exists():
            continue
        for path in directory.glob("*-funding.parquet"):
            symbol = path.name.split("-funding.parquet")[0].upper()
            frame = pd.read_parquet(path)
            if frame.empty:
                continue
            frame = frame.sort_values("timestamp_ms").copy()
            frame["funding_available_ms"] = frame.timestamp_ms.astype("int64") + 60_000
            merged = (
                frames[symbol]
                if symbol in frames
                else frame[["funding_available_ms", "last_funding_rate"]]
            )
            if symbol in frames:
                merged = pd.concat(
                    [merged, frame[["funding_available_ms", "last_funding_rate"]]],
                    ignore_index=True,
                ).drop_duplicates("funding_available_ms", keep="last")
            frames[symbol] = merged
    return frames


def merge_funding(panel: pd.DataFrame, funding: dict[str, pd.DataFrame]) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for symbol, scoped in panel.groupby("symbol", sort=False):
        funding_frame = funding.get(symbol.upper())
        if funding_frame is None or funding_frame.empty:
            continue
        merged = pd.merge_asof(
            scoped.sort_values("available_ms"),
            funding_frame.sort_values("funding_available_ms"),
            left_on="available_ms",
            right_on="funding_available_ms",
            direction="backward",
        )
        merged["funding_rate_pct"] = (
            pd.to_numeric(merged.last_funding_rate, errors="coerce") * 100
        )
        merged["funding_age_hours"] = (
            merged.available_ms - merged.funding_available_ms
        ) / 3_600_000
        parts.append(
            merged.drop(columns=["funding_available_ms", "last_funding_rate"])
        )
    return (
        pd.concat(parts, ignore_index=True)
        .sort_values(["available_ms", "symbol"])
        .reset_index(drop=True)
    )


def signals_for_profile(panel: pd.DataFrame, profile: Profile) -> pd.DataFrame:
    eligible = panel.loc[
        panel.symbol_age_days.ge(profile.min_age_days)
        & panel.liquidity_24h.ge(profile.min_liquidity_24h)
        & panel.funding_rate_pct.abs().ge(profile.min_abs_funding_pct)
        & panel.funding_age_hours.between(0, 12)
    ].copy()
    if eligible.empty:
        return eligible
    eligible["direction"] = np.where(
        eligible.funding_rate_pct.gt(0),
        "SHORT",
        "LONG",
    )
    eligible["abs_funding_pct"] = eligible.funding_rate_pct.abs()
    eligible["strength"] = eligible.groupby(
        "available_ms",
        sort=False,
    )["abs_funding_pct"].rank(pct=True)
    return (
        eligible.sort_values(
            ["available_ms", "abs_funding_pct", "liquidity_24h"],
            ascending=[True, False, False],
        )
        .groupby("available_ms", sort=False)
        .head(1)
        .sort_values("available_ms")
        .reset_index(drop=True)
    )


def simulate(
    signals: pd.DataFrame,
    panel: pd.DataFrame,
    profile: Profile,
    one_way_cost: float,
) -> pd.DataFrame:
    bars = {
        symbol: scoped.sort_values("available_ms").set_index("available_ms")
        for symbol, scoped in panel.groupby("symbol", sort=False)
    }
    trades: list[dict[str, Any]] = []
    active_exit = -1
    for signal in signals.itertuples(index=False):
        if int(signal.available_ms) < active_exit:
            continue
        scoped = bars.get(signal.symbol)
        if scoped is None:
            continue
        entry_bar_available_ms = int(signal.available_ms) + 3_600_000
        if entry_bar_available_ms not in scoped.index:
            continue
        start = int(scoped.index.searchsorted(entry_bar_available_ms))
        path = scoped.iloc[start : start + profile.hold_hours]
        if path.empty:
            continue
        entry = float(path.iloc[0].open)
        atr = float(signal.atr_24h)
        if not np.isfinite(entry) or entry <= 0 or not np.isfinite(atr) or atr <= 0:
            continue
        sign = 1.0 if signal.direction == "LONG" else -1.0
        stop_distance = profile.stop_pct if profile.stop_pct else 0.0
        stop = entry - sign * entry * stop_distance if stop_distance else None
        take = (
            entry + sign * entry * stop_distance * profile.take_r
            if stop_distance and profile.take_r
            else None
        )
        exit_price = float(path.iloc[-1].close)
        exit_ms = int(path.index[-1]) + 3_600_000
        outcome = "time"
        for timestamp, bar in path.iterrows():
            high = float(bar.high)
            low = float(bar.low)
            if stop is not None and (low <= stop if sign > 0 else high >= stop):
                exit_price = float(stop)
                exit_ms = int(timestamp) + 3_600_000
                outcome = "stop"
                break
            if take is not None and (high >= take if sign > 0 else low <= take):
                exit_price = float(take)
                exit_ms = int(timestamp) + 3_600_000
                outcome = "take"
                break
        gross_return = sign * (exit_price / entry - 1.0)
        trades.append(
            {
                "symbol": signal.symbol,
                "direction": signal.direction,
                "entry_ms": int(entry_bar_available_ms),
                "exit_ms": exit_ms,
                "gross_return": float(gross_return),
                "net_return": float(gross_return - 2.0 * one_way_cost),
                "outcome": outcome,
                "funding_rate_pct": float(signal.funding_rate_pct),
            }
        )
        active_exit = exit_ms
    return pd.DataFrame(trades)


def profit_factor(values: Iterable[float]) -> float:
    rows = [float(value) for value in values]
    gains = sum(value for value in rows if value > 0)
    losses = -sum(value for value in rows if value < 0)
    return gains / losses if losses > 0 else (999.0 if gains > 0 else 0.0)


def metrics(trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty:
        return {"trades": 0, "win_rate": 0.0, "profit_factor": 0.0, "net_return": 0.0}
    returns = trades.net_return
    return {
        "trades": int(len(trades)),
        "win_rate": round(float((returns > 0).mean()), 6),
        "profit_factor": round(profit_factor(returns), 6),
        "net_return": round(float(returns.sum()), 6),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    funding = load_funding(FUNDING_DIRS)
    print(f"funding symbols loaded: {len(funding)}", flush=True)
    panel_parts: list[pd.DataFrame] = []
    for data_dir in DATA_DIRS:
        starts, _ = load_manifest(data_dir)
        panel = build_panel(data_dir, starts)
        panel = panel.loc[panel.symbol.isin(funding)]
        panel_parts.append(panel)
        del panel
        gc.collect()
    panel = pd.concat(panel_parts, ignore_index=True).reset_index(drop=True)
    print(f"panel rows: {len(panel)}", flush=True)
    panel = merge_funding(panel, funding)
    print(f"merged rows with funding: {len(panel)}", flush=True)

    reports: dict[str, Any] = {}
    for profile in PROFILES:
        signals = signals_for_profile(panel, profile)
        base = simulate(signals, panel, profile, BASE_ONE_WAY_COST)
        stress = simulate(signals, panel, profile, STRESS_ONE_WAY_COST)
        base["year"] = pd.to_datetime(base.entry_ms, unit="ms", utc=True).dt.year
        stress["year"] = pd.to_datetime(stress.entry_ms, unit="ms", utc=True).dt.year
        by_window: dict[str, Any] = {}
        for window, (start, end) in WINDOWS.items():
            mask = base.year.ge(pd.to_datetime(start).year) & base.year.lt(
                pd.to_datetime(end).year
            )
            by_window[window] = {
                "base": metrics(base.loc[mask]),
                "stress": metrics(stress.loc[mask]),
            }
        reports[profile.name] = {
            "profile": asdict(profile),
            "windows": by_window,
        }
        dev = by_window["development_2020_2022"]["stress"]
        print(
            f"{profile.name}: dev_n={dev['trades']} dev_PF={dev['profit_factor']:.3f}",
            flush=True,
        )

    # Selection on development only.
    def dev_score(name: str) -> tuple[int, float, float]:
        dev = reports[name]["windows"]["development_2020_2022"]["stress"]
        return (int(dev["net_return"] > 0), float(dev["net_return"]), float(dev["profit_factor"]))

    ranked = sorted(reports, key=dev_score, reverse=True)
    selected = ranked[0]
    selected_profile = next(profile for profile in PROFILES if profile.name == selected)
    selected_stress = {
        window: reports[selected]["windows"][window]["stress"]
        for window in ("test_2024", "blind_2025", "final_2026")
    }
    selected_trades = simulate(
        signals_for_profile(panel, selected_profile),
        panel,
        selected_profile,
        STRESS_ONE_WAY_COST,
    )
    oos = selected_trades.loc[
        pd.to_datetime(selected_trades.entry_ms, unit="ms", utc=True).ge("2024-01-01")
    ].copy()
    top = oos.groupby("symbol").net_return.sum().nlargest(3).index
    without_top3 = metrics(oos.loc[~oos.symbol.isin(top)])
    qualified = bool(
        selected_stress["test_2024"]["trades"] >= 20
        and all(selected_stress[w]["profit_factor"] > 1.0 for w in selected_stress)
        and selected_stress["test_2024"]["profit_factor"] > 1.2
        and without_top3["net_return"] > 0
    )
    result = {
        "experiment": "s0_funding_crowding_reversal",
        "selected_profile": selected,
        "reports": reports,
        "selected_oos_stress": {k: selected_stress[k] for k in selected_stress},
        "selected_oos_without_top3": without_top3,
        "qualified": qualified,
        "warning": "Historical qualification is not live approval.",
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
