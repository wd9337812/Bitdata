from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_s0_market_tsmom_executable import asset_daily
from scripts.benchmark_s0_market_tsmom_28d import (
    DEFAULT_DATA,
    STRESS_ONE_WAY_COST,
    build_daily_panel,
    market_state,
)
from scripts.benchmark_s0_market_tsmom_consensus import consensus_state


OUTPUT = ROOT / "data" / "research" / "s0_executable_major_rotation"
TRAIN_END_YEAR = 2023
UNIVERSE = ("BNBUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "SOLUSDT", "AVAXUSDT")
RULES = {
    "BNBUSDT": {"min_qty": 0.01, "qty_step": 0.01, "min_notional": 5.0},
    "XRPUSDT": {"min_qty": 0.1, "qty_step": 0.1, "min_notional": 5.0},
    "ADAUSDT": {"min_qty": 1.0, "qty_step": 1.0, "min_notional": 5.0},
    "DOGEUSDT": {"min_qty": 1.0, "qty_step": 1.0, "min_notional": 5.0},
    "SOLUSDT": {"min_qty": 0.01, "qty_step": 0.01, "min_notional": 5.0},
    "AVAXUSDT": {"min_qty": 1.0, "qty_step": 1.0, "min_notional": 5.0},
}


@dataclass(frozen=True)
class ExitProfile:
    name: str
    stop_pct: float
    max_hold_days: int
    atr_multiple: float | None = None


PROFILES = (
    ExitProfile("time5_stop10", 0.10, 5),
    ExitProfile("trail_atr3_stop15_hold20", 0.15, 20, 3.0),
)
SELECTORS = (
    "liquidity",
    "momentum_28",
    "momentum_blend",
    "low_vol_positive",
    *(f"fixed_{symbol}" for symbol in UNIVERSE),
)


def _rank_score(group: pd.DataFrame, column: str, *, ascending: bool = True) -> pd.Series:
    return group[column].rank(pct=True, ascending=ascending, method="average")


def build_features(panel: pd.DataFrame) -> pd.DataFrame:
    frame = panel.loc[panel.symbol.isin(UNIVERSE)].copy().sort_values(["symbol", "day"])
    grouped = frame.groupby("symbol", group_keys=False)
    frame["age_days"] = grouped.cumcount() + 1
    frame["momentum_28"] = grouped.close.pct_change(28)
    frame["momentum_56"] = grouped.close.pct_change(56)
    frame["volatility_28"] = grouped["return"].rolling(28, min_periods=28).std().reset_index(level=0, drop=True)
    frame["liquidity_rank"] = frame.groupby("day", group_keys=False).apply(
        lambda group: _rank_score(group, "median_volume_30d"),
        include_groups=False,
    ).reset_index(level=0, drop=True)
    frame["momentum_28_rank"] = frame.groupby("day", group_keys=False).apply(
        lambda group: _rank_score(group, "momentum_28"),
        include_groups=False,
    ).reset_index(level=0, drop=True)
    frame["momentum_56_rank"] = frame.groupby("day", group_keys=False).apply(
        lambda group: _rank_score(group, "momentum_56"),
        include_groups=False,
    ).reset_index(level=0, drop=True)
    frame["blend_score"] = 0.5 * frame.momentum_28_rank + 0.5 * frame.momentum_56_rank
    return frame


def select_symbol(group: pd.DataFrame, selector: str) -> str | None:
    eligible = group.loc[
        group.age_days.ge(365)
        & group.momentum_28.notna()
        & group.momentum_56.notna()
        & group.volatility_28.notna()
        & group.median_volume_30d.gt(0)
    ].copy()
    if eligible.empty:
        return None
    if selector.startswith("fixed_"):
        symbol = selector.removeprefix("fixed_")
        return symbol if symbol in set(eligible.symbol) else None
    if selector == "liquidity":
        chosen = eligible.sort_values(["median_volume_30d", "symbol"], ascending=[False, True])
    elif selector == "momentum_28":
        chosen = eligible.sort_values(["momentum_28", "median_volume_30d"], ascending=[False, False])
    elif selector == "momentum_blend":
        chosen = eligible.sort_values(["blend_score", "median_volume_30d"], ascending=[False, False])
    elif selector == "low_vol_positive":
        positive = eligible.loc[eligible.momentum_28.gt(0) & eligible.momentum_56.gt(0)]
        if positive.empty:
            return None
        chosen = positive.sort_values(["volatility_28", "median_volume_30d"], ascending=[True, False])
    else:
        raise ValueError(f"unknown selector: {selector}")
    return str(chosen.iloc[0].symbol)


def simulate_rotation(
    panel: pd.DataFrame,
    features: pd.DataFrame,
    state: pd.DataFrame,
    selector: str,
    profile: ExitProfile,
    *,
    delay_days: int = 0,
) -> pd.DataFrame:
    assets = {symbol: asset_daily(panel, symbol).set_index("day") for symbol in UNIVERSE}
    feature_days = {day: group for day, group in features.groupby("day")}
    market = state.set_index("day")
    days = sorted(set(market.index).intersection(feature_days))
    rows: list[dict[str, Any]] = []
    next_available = pd.Timestamp.min.tz_localize("UTC")
    for signal_day in days:
        if signal_day < next_available or not bool(market.loc[signal_day, "signal"]):
            continue
        symbol = select_symbol(feature_days[signal_day], selector)
        if not symbol:
            continue
        asset = assets[symbol]
        if signal_day not in asset.index:
            continue
        signal_loc = asset.index.get_loc(signal_day)
        entry_loc = signal_loc + 1 + delay_days
        if entry_loc >= len(asset):
            continue
        entry = asset.iloc[entry_loc]
        entry_day = asset.index[entry_loc]
        entry_price = float(entry.open)
        atr = float(asset.iloc[signal_loc].atr_10)
        if not math.isfinite(atr) or atr <= 0 or entry_price <= 0:
            continue
        stop = entry_price * (1.0 - profile.stop_pct)
        if profile.atr_multiple is not None:
            stop = max(stop, float(asset.iloc[signal_loc].close) - profile.atr_multiple * atr)
        initial_stop = stop
        exit_loc = min(entry_loc + profile.max_hold_days, len(asset) - 1)
        exit_price = float(asset.iloc[exit_loc].open)
        reason = "max_hold"
        peak_close = float(asset.iloc[signal_loc].close)
        for path_loc in range(entry_loc, exit_loc + 1):
            bar = asset.iloc[path_loc]
            if float(bar.open) <= stop:
                exit_loc, exit_price, reason = path_loc, float(bar.open), "gap_stop"
                break
            if float(bar.low) <= stop:
                exit_loc, exit_price, reason = path_loc, stop, "stop"
                break
            if profile.atr_multiple is not None:
                market_day = asset.index[path_loc - 1] if path_loc > entry_loc else signal_day
                if market_day in market.index and not bool(market.loc[market_day, "signal"]):
                    exit_loc, exit_price, reason = path_loc, float(bar.open), "market_signal_off"
                    break
                peak_close = max(peak_close, float(bar.close))
                path_atr = float(bar.atr_10)
                if math.isfinite(path_atr) and path_atr > 0:
                    stop = max(stop, peak_close - profile.atr_multiple * path_atr)
        exit_day = asset.index[exit_loc]
        rows.append(
            {
                "selector": selector,
                "profile": profile.name,
                "signal_day": signal_day,
                "entry_day": entry_day,
                "exit_day": exit_day,
                "symbol": symbol,
                "entry_price": entry_price,
                "initial_stop_pct": max(0.0, (entry_price - initial_stop) / entry_price),
                "net_return": exit_price / entry_price - 1.0 - 2.0 * STRESS_ONE_WAY_COST,
                "exit_reason": reason,
            }
        )
        next_available = exit_day
    return pd.DataFrame(rows)


def _floor_step(value: float, step: float) -> float:
    return math.floor((value + 1e-12) / step) * step if value > 0 and step > 0 else 0.0


def executable_metrics(
    trades: pd.DataFrame,
    *,
    starting_equity: float,
    risk_pct: float,
    leverage: float = 2.0,
    hard_stop: float = 5.0,
    reserve: float = 0.5,
) -> dict[str, Any]:
    equity = float(starting_equity)
    peak = equity
    drawdown = 0.0
    executed: list[dict[str, float | str]] = []
    skipped = 0
    for trade in trades.sort_values("entry_day").itertuples(index=False):
        rules = RULES[trade.symbol]
        stop_pct = max(1e-9, float(trade.initial_stop_pct))
        risk_budget = min(equity * risk_pct, max(0.0, equity - hard_stop - reserve))
        notional_target = min(risk_budget / stop_pct, equity * 0.90 * leverage)
        qty = _floor_step(notional_target / float(trade.entry_price), rules["qty_step"])
        notional = qty * float(trade.entry_price)
        if qty < rules["min_qty"] or notional < rules["min_notional"]:
            skipped += 1
            continue
        pnl = notional * float(trade.net_return)
        equity += pnl
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1.0)
        executed.append({"pnl": pnl, "symbol": trade.symbol})
        if equity <= hard_stop:
            break
    wins = sum(max(0.0, float(row["pnl"])) for row in executed)
    losses = sum(max(0.0, -float(row["pnl"])) for row in executed)
    return {
        "start": starting_equity,
        "end": round(equity, 6),
        "return_pct": round((equity / starting_equity - 1.0) * 100.0, 4),
        "executed": len(executed),
        "skipped": skipped,
        "win_rate_pct": round(sum(float(row["pnl"]) > 0 for row in executed) / max(1, len(executed)) * 100.0, 4),
        "profit_factor": round(wins / losses, 4) if losses else (999.0 if wins else 0.0),
        "max_drawdown_pct": round(drawdown * 100.0, 4),
        "hard_stopped": equity <= hard_stop,
    }


def training_score(report: dict[str, Any]) -> tuple[float, float, int]:
    annual = report["annual"]
    return (
        min((float(row["return_pct"]) for row in annual.values()), default=-999.0),
        float(report["overall"]["profit_factor"]),
        int(report["overall"]["executed"]),
    )


def period_report(trades: pd.DataFrame, starting_equity: float, risk_pct: float) -> dict[str, Any]:
    years = pd.to_datetime(trades.entry_day, utc=True).dt.year
    overall = executable_metrics(trades, starting_equity=starting_equity, risk_pct=risk_pct)
    annual = {
        str(year): executable_metrics(
            trades.loc[years.eq(year)], starting_equity=starting_equity, risk_pct=risk_pct
        )
        for year in sorted(years.unique())
    }
    return {"overall": overall, "annual": annual}


def bootstrap_net_return(trades: pd.DataFrame, *, samples: int = 10_000) -> dict[str, float]:
    values = trades.net_return.astype(float).to_numpy()
    if not len(values):
        return {"positive_probability_pct": 0.0, "p05": 0.0, "median": 0.0, "p95": 0.0}
    rng = np.random.default_rng(20260803)
    totals = rng.choice(values, size=(samples, len(values)), replace=True).sum(axis=1)
    return {
        "positive_probability_pct": round(float((totals > 0).mean() * 100.0), 4),
        "p05": round(float(np.quantile(totals, 0.05)), 6),
        "median": round(float(np.quantile(totals, 0.50)), 6),
        "p95": round(float(np.quantile(totals, 0.95)), 6),
    }


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    panel = build_daily_panel(DEFAULT_DATA)
    features = build_features(panel)
    state = consensus_state(market_state(panel), 56)
    candidates: dict[str, Any] = {}
    trades_by_name: dict[str, pd.DataFrame] = {}
    for selector in SELECTORS:
        for profile in PROFILES:
            name = f"{selector}__{profile.name}"
            trades = simulate_rotation(panel, features, state, selector, profile)
            trades_by_name[name] = trades
            years = pd.to_datetime(trades.entry_day, utc=True).dt.year
            candidates[name] = {
                "train": period_report(trades.loc[years.le(TRAIN_END_YEAR)], 15.153, 0.15),
                "oos": period_report(trades.loc[years.gt(TRAIN_END_YEAR)], 15.153, 0.15),
            }
    selected = max(candidates, key=lambda name: training_score(candidates[name]["train"]))
    selected_trades = trades_by_name[selected]
    selected_trades.to_parquet(OUTPUT / "selected_trades.parquet", index=False)
    years = pd.to_datetime(selected_trades.entry_day, utc=True).dt.year
    robustness = {}
    for capital in (15.153, 25.153, 50.0):
        for risk in (0.10, 0.15, 0.20, 0.30):
            key = f"capital_{capital:g}_risk_{int(risk * 100)}"
            robustness[key] = {
                "train": period_report(selected_trades.loc[years.le(TRAIN_END_YEAR)], capital, risk),
                "oos": period_report(selected_trades.loc[years.gt(TRAIN_END_YEAR)], capital, risk),
            }
    delays = {}
    selector, profile_name = selected.split("__", 1)
    profile = next(item for item in PROFILES if item.name == profile_name)
    for delay in (0, 1, 2):
        delayed = simulate_rotation(panel, features, state, selector, profile, delay_days=delay)
        delayed_years = pd.to_datetime(delayed.entry_day, utc=True).dt.year
        delays[str(delay)] = period_report(delayed.loc[delayed_years.gt(TRAIN_END_YEAR)], 25.153, 0.15)

    fixed_time_candidates = [
        name
        for name in candidates
        if name.startswith("fixed_") and name.endswith("__time5_stop10")
    ]

    def fixed_training_score(name: str) -> tuple[int, float, float]:
        report = candidates[name]["train"]
        annual_returns = [
            float(row["return_pct"]) for row in report["annual"].values()
        ]
        return (
            sum(value > 0 for value in annual_returns),
            min(annual_returns, default=-999.0),
            float(report["overall"]["profit_factor"]),
        )

    fixed_selected = max(fixed_time_candidates, key=fixed_training_score)
    fixed_trades = trades_by_name[fixed_selected]
    fixed_years = pd.to_datetime(fixed_trades.entry_day, utc=True).dt.year
    fixed_robustness: dict[str, Any] = {}
    for capital in (15.153, 25.153, 50.0):
        for risk in (0.10, 0.15, 0.20, 0.30):
            key = f"capital_{capital:g}_risk_{int(risk * 100)}"
            fixed_robustness[key] = {
                "train": period_report(
                    fixed_trades.loc[fixed_years.le(TRAIN_END_YEAR)], capital, risk
                ),
                "oos": period_report(
                    fixed_trades.loc[fixed_years.gt(TRAIN_END_YEAR)], capital, risk
                ),
            }
    fixed_selector, fixed_profile_name = fixed_selected.split("__", 1)
    fixed_profile = next(item for item in PROFILES if item.name == fixed_profile_name)
    fixed_delays = {}
    for delay in (0, 1, 2):
        delayed = simulate_rotation(
            panel, features, state, fixed_selector, fixed_profile, delay_days=delay
        )
        delayed_years = pd.to_datetime(delayed.entry_day, utc=True).dt.year
        fixed_delays[str(delay)] = period_report(
            delayed.loc[delayed_years.gt(TRAIN_END_YEAR)], 25.153, 0.15
        )
    fixed_oos = fixed_trades.loc[fixed_years.gt(TRAIN_END_YEAR)].copy()
    fixed_cost_stress = {}
    for multiplier in (1.0, 1.5, 2.0):
        stressed = fixed_oos.copy()
        stressed["net_return"] = stressed.net_return - (
            2.0 * STRESS_ONE_WAY_COST * (multiplier - 1.0)
        )
        fixed_cost_stress[f"{multiplier:g}x"] = {
            "report": period_report(stressed, 25.153, 0.15),
            "bootstrap": bootstrap_net_return(stressed),
        }
    result = {
        "experiment": "s0_exchange_executable_major_rotation",
        "universe": list(UNIVERSE),
        "selection": "predeclared selectors and exits ranked only on 2020-2023 worst annual executable return",
        "stress_one_way_cost_pct": STRESS_ONE_WAY_COST * 100.0,
        "selected": selected,
        "selected_result": candidates[selected],
        "robustness": robustness,
        "oos_entry_delay_days": delays,
        "fixed_asset_selection": {
            "method": "among fixed mature assets, maximize positive training years, then worst training year, then PF; never use OOS",
            "selected": fixed_selected,
            "selected_result": candidates[fixed_selected],
            "robustness": fixed_robustness,
            "oos_entry_delay_days": fixed_delays,
            "oos_cost_stress": fixed_cost_stress,
        },
        "candidates": candidates,
    }
    (OUTPUT / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "candidates"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
