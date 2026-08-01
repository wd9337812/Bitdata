from __future__ import annotations

import argparse
import gc
import hashlib
import heapq
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_s0_xmom_regime_candidates import block_bootstrap  # noqa: E402
from scripts.benchmark_s0_cross_sectional_momentum import (  # noqa: E402
    Profile,
    simulate,
    summarize,
)
from scripts.benchmark_s0_point_in_time_slow_momentum import (  # noqa: E402
    CANDIDATES,
    add_slow_returns,
    slow_momentum_signals,
)
from scripts.benchmark_s0_xmom_point_in_time import (  # noqa: E402
    build_panel,
    load_manifest,
)


DEFAULT_DATA = (
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2020_2023",
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2024_2025",
    ROOT / "data" / "research" / "binance_um_point_in_time_1h",
)
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_30d_momentum_adaptive_v2"
BASE_COST_PCT = 0.36
STRESS_COST_PCT = 0.60
BREADTH_LOW = 0.02
BREADTH_HIGH = 0.10
HISTORY_SIZE = 8
MIN_HISTORY = 3
MIN_HISTORY_PF = 1.25


def adaptive_profile() -> Profile:
    return Profile(
        "momentum_30d_adaptive_exit_v2",
        ("ret_720h",),
        0.05,
        2.5,
        2.5,
        120,
        12.0,
    )


def profit_factor(values: list[float] | pd.Series) -> float:
    frame = pd.Series(values, dtype="float64")
    wins = float(frame.loc[frame > 0].sum())
    losses = float(-frame.loc[frame < 0].sum())
    if losses == 0:
        return 999.0 if wins > 0 else 0.0
    return wins / losses


def apply_direction_gate(
    trades: pd.DataFrame,
    initial_history: dict[str, list[float]] | None = None,
) -> pd.DataFrame:
    """Use only outcomes that were closed before the next decision.

    Every candidate updates the paper history, including candidates that the
    gate would not fund. This lets a blocked direction recover without using
    live capital and prevents a permanent lockout.
    """
    history = {
        direction: list((initial_history or {}).get(direction, []))
        for direction in ("LONG", "SHORT")
    }
    ordered = trades.sort_values("entry_ms").reset_index(drop=True)
    selected: list[int] = []
    last_exit_ms = -1
    for row in ordered.itertuples():
        if int(row.entry_ms) < last_exit_ms:
            raise ValueError("Candidate trades overlap; event-time gate is ambiguous")
        recent = history[str(row.direction)][-HISTORY_SIZE:]
        allowed = len(recent) < MIN_HISTORY or profit_factor(recent) >= MIN_HISTORY_PF
        if allowed:
            selected.append(int(row.Index))
        history[str(row.direction)].append(float(row.net_pct))
        last_exit_ms = int(row.exit_ms)
    return ordered.loc[selected].reset_index(drop=True)


def apply_event_time_gate(
    trades: pd.DataFrame,
    initial_history: dict[str, list[float]] | None = None,
    symbol_embargo_hours: int = 0,
) -> pd.DataFrame:
    """Allocate one funded position while independent paper outcomes mature.

    Rejected paper candidates never occupy the funded account. Their outcomes
    enter direction history only after their hypothetical exits. The optional
    symbol embargo keeps one trend episode from becoming several funded bets.
    """
    if trades.empty:
        return trades.copy()
    history = {
        direction: list((initial_history or {}).get(direction, []))
        for direction in ("LONG", "SHORT")
    }
    ordered = trades.sort_values(
        ["entry_ms", "strength"], ascending=[True, False]
    ).reset_index(drop=True)
    pending: list[tuple[int, int, str, float]] = []
    selected: list[int] = []
    funded_exit_ms = -1
    embargo_until: dict[str, int] = {}
    embargo_ms = max(0, int(symbol_embargo_hours)) * 3_600_000

    for sequence, row in enumerate(ordered.itertuples()):
        entry_ms = int(row.entry_ms)
        while pending and pending[0][0] <= entry_ms:
            _, _, direction, net_pct = heapq.heappop(pending)
            history[direction].append(net_pct)

        direction = str(row.direction)
        recent = history[direction][-HISTORY_SIZE:]
        direction_allowed = (
            len(recent) < MIN_HISTORY or profit_factor(recent) >= MIN_HISTORY_PF
        )
        account_available = entry_ms >= funded_exit_ms
        symbol_available = entry_ms >= embargo_until.get(str(row.symbol), -1)
        if direction_allowed and account_available and symbol_available:
            selected.append(int(row.Index))
            funded_exit_ms = int(row.exit_ms)
            embargo_until[str(row.symbol)] = funded_exit_ms + embargo_ms

        heapq.heappush(
            pending,
            (int(row.exit_ms), sequence, direction, float(row.net_pct)),
        )

    return ordered.loc[selected].reset_index(drop=True)


def cohort(symbol: str) -> str:
    bucket = int(hashlib.sha256(symbol.encode("utf-8")).hexdigest()[:8], 16) % 10
    return "blind" if bucket >= 8 else "other"


def annual(frame: pd.DataFrame) -> dict[str, dict[str, Any]]:
    if frame.empty:
        return {}
    years = pd.to_datetime(frame.entry_ms, unit="ms", utc=True).dt.year
    return {
        str(int(year)): summarize(group)
        for year, group in frame.assign(year=years).groupby("year", sort=True)
    }


def adjusted_cost(frame: pd.DataFrame, cost_pct: float) -> pd.DataFrame:
    result = frame.copy()
    result["cost_pct"] = float(cost_pct)
    result["net_pct"] = result.gross_pct - float(cost_pct)
    return result


def report(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {"overall": summarize(frame), "annual": {}, "cohorts": {}}
    top = frame.groupby("symbol").net_pct.sum().nlargest(3).index
    cohorts = frame.assign(cohort=frame.symbol.map(cohort)).groupby("cohort", sort=True)
    return {
        "overall": summarize(frame),
        "annual": annual(frame),
        "cohorts": {name: summarize(group) for name, group in cohorts},
        "without_top_3_symbols": summarize(frame.loc[~frame.symbol.isin(top)]),
        "bootstrap": block_bootstrap(frame),
    }


def qualifies(base: dict[str, Any], stress: dict[str, Any]) -> bool:
    required_years = {"2021", "2022", "2023", "2024", "2025", "2026"}
    if not required_years.issubset(base["annual"]):
        return False
    if any(
        base["annual"][year].get("profit_factor", 0) <= 1.0
        or base["annual"][year].get("net_pct_points", 0) <= 0
        for year in required_years
    ):
        return False
    if any(
        stress["annual"][year].get("net_pct_points", 0) <= 0
        for year in required_years
    ):
        return False
    blind = stress["cohorts"].get("blind", {})
    return bool(
        stress["overall"].get("trades", 0) >= 60
        and stress["overall"].get("profit_factor", 0) > 1.2
        and blind.get("trades", 0) >= 15
        and blind.get("net_pct_points", 0) > 0
        and stress["without_top_3_symbols"].get("net_pct_points", 0) > 0
        and stress["bootstrap"].get("positive_probability", 0) >= 0.95
    )


def build_source_trades(data_dirs: tuple[Path, ...]) -> tuple[pd.DataFrame, pd.DataFrame]:
    candidate = next(item for item in CANDIDATES if item.name == "momentum_30d_hold_7d")
    parts: list[pd.DataFrame] = []
    signal_parts: list[pd.DataFrame] = []
    for data_dir in data_dirs:
        starts, _ = load_manifest(data_dir)
        panel = add_slow_returns(build_panel(data_dir, starts))
        signals = slow_momentum_signals(panel, candidate, minimum_age_days=45)
        signals = signals.loc[
            signals.market_breadth.abs().between(BREADTH_LOW, BREADTH_HIGH)
        ]
        signal_parts.append(signals.copy())
        parts.append(simulate(signals, panel, adaptive_profile(), cost_pct=BASE_COST_PCT))
        del panel, signals
        gc.collect()
    trades = pd.concat(parts, ignore_index=True).sort_values("entry_ms").reset_index(drop=True)
    all_signals = (
        pd.concat(signal_parts, ignore_index=True)
        .sort_values(["available_ms", "symbol"])
        .drop_duplicates(["available_ms", "symbol", "direction"], keep="last")
        .reset_index(drop=True)
    )
    return trades, all_signals


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit adaptive 30-day alt momentum.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    source_path = args.output / "all_trades.parquet"
    signals_path = args.output / "signals_all.parquet"
    if args.rebuild or not source_path.exists() or not signals_path.exists():
        source, all_signals = build_source_trades(DEFAULT_DATA)
        all_signals.to_parquet(signals_path, index=False)
        signal_years = pd.to_datetime(all_signals.available_ms, unit="ms", utc=True).dt.year
        all_signals.loc[signal_years.eq(2026)].to_parquet(
            args.output / "signals_2026.parquet",
            index=False,
        )
    else:
        source = pd.read_parquet(source_path)
    source.to_parquet(source_path, index=False)
    funded = apply_direction_gate(source)
    stress = adjusted_cost(funded, STRESS_COST_PCT)
    reports = {"base": report(funded), "stress": report(stress)}
    accepted = qualifies(reports["base"], reports["stress"])
    funded.to_parquet(args.output / "funded_trades_base.parquet", index=False)
    stress.to_parquet(args.output / "funded_trades_stress.parquet", index=False)
    result = {
        "experiment": "s0_adaptive_30d_momentum",
        "source": "Binance official point-in-time USD-M 1h archives",
        "candidate": {
            "formation_days": 30,
            "cadence_hours": 24,
            "maximum_hold_days": 5,
            "stop_atr": 2.5,
            "reward_r": 2.5,
            "maximum_stop_pct": 12.0,
            "breadth_abs_band": [BREADTH_LOW, BREADTH_HIGH],
            "history_size_per_direction": HISTORY_SIZE,
            "minimum_history": MIN_HISTORY,
            "minimum_trailing_profit_factor": MIN_HISTORY_PF,
        },
        "reports": reports,
        "qualified_for_independent_forward_shadow": accepted,
        "decision": (
            "historically_positive_requires_forward_shadow"
            if accepted
            else "research_only_not_eligible"
        ),
        "warning": (
            "This candidate was found during retrospective research. Historical "
            "qualification is not an untouched future test and is not live approval."
        ),
    }
    (args.output / "report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
