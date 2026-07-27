from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HISTORY = ROOT / "data" / "research" / "vps_history_20260727"
DEFAULT_KLINES = ROOT / "data" / "research" / "vps_replay_1m_20260727" / "parquet"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "vps_strategy_replay_20260727"
VERSIONS = ("v4.10", "v4.11", "v4.7.2", "v4.7.3", "v4.7.4")
VALIDATION_START = pd.Timestamp("2026-07-24T00:00:00Z")
TEST_START = pd.Timestamp("2026-07-26T00:00:00Z")


@dataclass(frozen=True)
class ExitProfile:
    name: str
    stop_scale: float
    reward_risk: float
    max_hold_minutes: int


PROFILES = tuple(
    ExitProfile(
        name=f"stop_{stop_scale:.2f}_rr_{reward_risk:.2f}_hold_{hold}",
        stop_scale=stop_scale,
        reward_risk=reward_risk,
        max_hold_minutes=hold,
    )
    for stop_scale in (0.85, 1.00, 1.15)
    for reward_risk in (0.75, 1.00, 1.2857, 1.50)
    for hold in (3, 5, 8, 10)
)


def _metrics(values: pd.Series, name: str) -> dict[str, Any]:
    values = values.dropna().astype(float)
    if values.empty:
        return {"name": name, "trades": 0}
    gains = float(values[values > 0].sum())
    losses = float(-values[values < 0].sum())
    curve = values.cumsum()
    drawdown = curve.cummax() - curve
    return {
        "name": name,
        "trades": int(len(values)),
        "win_rate": round(float((values > 0).mean() * 100.0), 4),
        "net_pct_points": round(float(values.sum()), 6),
        "mean_net_pct": round(float(values.mean()), 6),
        "profit_factor": round(gains / losses, 4) if losses > 0 else (999.0 if gains > 0 else 0.0),
        "max_drawdown_pct_points": round(float(drawdown.max()), 6),
    }


def _load_shadow(history: Path) -> pd.DataFrame:
    frame = pd.read_csv(history / "shadow_trades_compact.csv.gz", low_memory=False)
    frame = frame[
        frame.strategy_family.eq("extreme_v4_roll")
        & frame.strategy_version.isin(VERSIONS)
        & frame.evidence_type.eq("decision")
        & frame.status.eq("CLOSED")
    ].copy()
    frame["time"] = pd.to_datetime(frame.opened_at, utc=True)
    frame["opened_ms"] = frame.time.map(
        lambda value: int(value.timestamp() * 1_000)
    ).astype("int64")
    frame["expiry_minutes"] = (
        pd.to_datetime(frame.expires_at, utc=True) - frame.time
    ).dt.total_seconds().div(60.0)
    frame["cost_pct"] = (
        pd.to_numeric(frame.estimated_cost, errors="coerce")
        .div(pd.to_numeric(frame.notional, errors="coerce"))
        .mul(100.0)
        .fillna(0.12)
        .clip(lower=0.08, upper=0.35)
    )
    frame["original_net_pct"] = (
        pd.to_numeric(frame.net_pnl, errors="coerce")
        .div(pd.to_numeric(frame.notional, errors="coerce"))
        .mul(100.0)
    )
    frame["rank_percentile"] = pd.to_numeric(frame.rank_percentile, errors="coerce")
    frame["lower_expected_net_pct"] = pd.to_numeric(
        frame.lower_expected_net_pct, errors="coerce"
    )
    return frame.sort_values("opened_ms").reset_index(drop=True)


def _load_symbol_bars(directory: Path, symbol: str) -> pd.DataFrame | None:
    path = directory / f"{symbol}.parquet"
    if not path.exists():
        return None
    bars = pd.read_parquet(path, columns=["open_time", "open", "high", "low", "close"])
    return bars.sort_values("open_time").reset_index(drop=True)


def _replay_one_path(
    row: Any,
    path: pd.DataFrame,
    profile: ExitProfile,
) -> tuple[float, str, int] | None:
    entry = float(row.entry)
    original_stop = float(row.stop)
    if not np.isfinite(entry) or not np.isfinite(original_stop) or entry <= 0:
        return None
    risk_distance = abs(entry - original_stop) * profile.stop_scale
    if risk_distance <= 0:
        return None
    direction = str(row.direction).upper()
    stop = entry - risk_distance if direction == "LONG" else entry + risk_distance
    target_distance = risk_distance * profile.reward_risk
    target = entry + target_distance if direction == "LONG" else entry - target_distance
    path = path.iloc[: max(1, profile.max_hold_minutes)]
    if path.empty:
        return None
    exit_price = float(path.iloc[-1].close)
    outcome = "TIME"
    held = len(path)
    for index, bar in enumerate(path.itertuples(index=False), start=1):
        if direction == "LONG":
            stop_hit = float(bar.low) <= stop
            target_hit = float(bar.high) >= target
        else:
            stop_hit = float(bar.high) >= stop
            target_hit = float(bar.low) <= target
        # One-minute bars do not reveal intrabar ordering. When both barriers
        # are touched, use the adverse-first result to avoid optimistic bias.
        if stop_hit:
            exit_price, outcome, held = stop, "STOP", index
            break
        if target_hit:
            exit_price, outcome, held = target, "TAKE_PROFIT", index
            break
    gross = (
        (exit_price / entry - 1.0) * 100.0
        if direction == "LONG"
        else (entry / exit_price - 1.0) * 100.0
    )
    return gross - float(row.cost_pct), outcome, held


def replay(frame: pd.DataFrame, directory: Path, profiles: tuple[ExitProfile, ...]) -> pd.DataFrame:
    output: list[dict[str, Any]] = []
    for symbol, scoped in frame.groupby("symbol", sort=True):
        bars = _load_symbol_bars(directory, str(symbol))
        if bars is None:
            continue
        for row in scoped.itertuples():
            # The signal minute contains price action from before the decision.
            # Reuse the next ten complete bars for every profile.
            start = ((int(row.opened_ms) // 60_000) + 1) * 60_000
            left = int(bars.open_time.searchsorted(start, side="left"))
            path = bars.iloc[left : left + 10]
            if path.empty:
                continue
            base = {
                "id": int(row.id),
                "time": row.time,
                "opened_ms": int(row.opened_ms),
                "symbol": str(row.symbol),
                "direction": str(row.direction),
                "setup_type": str(row.setup_type),
                "market_regime": str(row.market_regime),
                "strategy_version": str(row.strategy_version),
                "rank_percentile": float(row.rank_percentile)
                if np.isfinite(row.rank_percentile)
                else np.nan,
                "lower_expected_net_pct": float(row.lower_expected_net_pct)
                if np.isfinite(row.lower_expected_net_pct)
                else np.nan,
                "original_net_pct": float(row.original_net_pct),
            }
            for profile in profiles:
                result = _replay_one_path(row, path, profile)
                if result is None:
                    continue
                net_pct, outcome, held = result
                output.append(
                    {
                        **base,
                        "profile": profile.name,
                        "net_pct": net_pct,
                        "outcome": outcome,
                        "held_minutes": held,
                    }
                )
    return pd.DataFrame(output)


def schedule(frame: pd.DataFrame, *, score_column: str) -> pd.DataFrame:
    if frame.empty:
        return frame
    ranked = frame.copy()
    ranked["batch"] = (ranked.opened_ms // 300_000).astype("int64")
    ranked["_score"] = pd.to_numeric(ranked[score_column], errors="coerce").fillna(-999.0)
    ranked = ranked.sort_values(
        ["batch", "_score", "rank_percentile"],
        ascending=[True, False, False],
    ).drop_duplicates("batch", keep="first")
    chosen: list[int] = []
    free_at = -1
    symbol_free_at: dict[tuple[str, str], int] = {}
    for row in ranked.sort_values("opened_ms").itertuples():
        timestamp = int(row.opened_ms)
        key = (str(row.symbol), str(row.direction))
        if timestamp < free_at or timestamp < symbol_free_at.get(key, -1):
            continue
        chosen.append(row.Index)
        hold_ms = max(1, int(row.held_minutes)) * 60_000
        free_at = timestamp + hold_ms
        symbol_free_at[key] = timestamp + 30 * 60_000
    return ranked.loc[chosen].sort_values("opened_ms")


def _window(frame: pd.DataFrame, name: str) -> pd.DataFrame:
    if name == "development":
        return frame[frame.time < VALIDATION_START]
    if name == "validation":
        return frame[(frame.time >= VALIDATION_START) & (frame.time < TEST_START)]
    if name == "test":
        return frame[frame.time >= TEST_START]
    raise ValueError(name)


def summarize(replayed: pd.DataFrame) -> dict[str, Any]:
    if replayed.empty:
        return {
            "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
            "versions": list(VERSIONS),
            "validation_start": VALIDATION_START.isoformat(),
            "test_start": TEST_START.isoformat(),
            "replayed_rows": 0,
            "unique_candidates": 0,
            "profiles": {},
            "validated_profiles": [],
            "decision": "no_replay_rows",
            "top_validation_profiles": [],
        }
    report: dict[str, Any] = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "versions": list(VERSIONS),
        "validation_start": VALIDATION_START.isoformat(),
        "test_start": TEST_START.isoformat(),
        "replayed_rows": int(len(replayed)),
        "unique_candidates": int(replayed.id.nunique()) if not replayed.empty else 0,
        "profiles": {},
    }
    for profile, scoped in replayed.groupby("profile"):
        detail: dict[str, Any] = {}
        for window in ("development", "validation", "test"):
            selected = schedule(_window(scoped, window), score_column="rank_percentile")
            detail[window] = _metrics(selected.net_pct, f"{profile}_{window}")
        report["profiles"][profile] = detail

    viable: list[tuple[float, float, str]] = []
    for profile, detail in report["profiles"].items():
        validation = detail["validation"]
        test = detail["test"]
        if (
            validation.get("trades", 0) >= 30
            and test.get("trades", 0) >= 20
            and validation.get("net_pct_points", 0.0) > 0
            and test.get("net_pct_points", 0.0) > 0
            and validation.get("profit_factor", 0.0) > 1.0
            and test.get("profit_factor", 0.0) > 1.0
        ):
            viable.append(
                (
                    float(validation["mean_net_pct"]),
                    float(test["mean_net_pct"]),
                    profile,
                )
            )
    viable.sort(reverse=True)
    report["validated_profiles"] = [
        {
            "profile": name,
            "validation_mean_net_pct": validation_mean,
            "test_mean_net_pct": test_mean,
            **report["profiles"][name],
        }
        for validation_mean, test_mean, name in viable
    ]
    report["decision"] = (
        "validated_exit_candidate"
        if viable
        else "no_exit_profile_passed_validation_and_test"
    )
    ranked = sorted(
        report["profiles"].items(),
        key=lambda item: (
            item[1]["validation"].get("mean_net_pct", -999.0),
            item[1]["test"].get("mean_net_pct", -999.0),
        ),
        reverse=True,
    )
    report["top_validation_profiles"] = [
        {"profile": name, **detail} for name, detail in ranked[:10]
    ]
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay VPS shadow opportunities against fresh Binance one-minute data."
    )
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--klines", type=Path, default=DEFAULT_KLINES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    shadow = _load_shadow(args.history)
    replayed = replay(shadow, args.klines, PROFILES)
    replayed.to_parquet(args.output / "replayed_candidates.parquet", index=False)
    report = summarize(replayed)
    (args.output / "strategy_replay_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
