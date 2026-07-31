from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DATA_DIRS = (
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2020_2023" / "parquet",
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2024_2025" / "parquet",
    ROOT / "data" / "research" / "binance_um_point_in_time_1h" / "parquet",
)
DEFAULT_METRICS = ROOT / "data" / "research" / "binance_um_metrics_1h"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_oi_flush"
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT", "DOGEUSDT", "ADAUSDT")
BASE_COST_PCT = 0.12
STRESS_COST_PCT = 0.24


@dataclass(frozen=True)
class Candidate:
    name: str
    setup: str
    lookback_hours: int
    minimum_price_move_pct: float
    minimum_oi_move_pct: float
    minimum_volume_ratio: float
    stop_pct: float
    target_pct: float
    hold_hours: int


CANDIDATES = (
    Candidate("flush_reversal_moderate", "flush_reversal", 3, 1.0, 1.0, 1.5, 3.0, 5.0, 12),
    Candidate("flush_reversal_strong", "flush_reversal", 6, 2.0, 2.0, 2.0, 4.0, 7.0, 24),
    Candidate("flush_continuation_moderate", "flush_continuation", 3, 1.0, 1.0, 1.5, 3.0, 5.0, 12),
    Candidate("flush_continuation_strong", "flush_continuation", 6, 2.0, 2.0, 2.0, 4.0, 7.0, 24),
    Candidate("build_breakout_moderate", "build_breakout", 3, 1.0, 1.0, 1.5, 3.0, 5.0, 12),
    Candidate("build_breakout_strong", "build_breakout", 6, 2.0, 2.0, 2.0, 4.0, 7.0, 24),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark pre-registered open-interest flush and build-up events."
    )
    parser.add_argument("--metrics", type=Path, default=DEFAULT_METRICS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    return parser.parse_args()


def load_bars(symbol: str) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for directory in DATA_DIRS:
        path = directory / f"{symbol}.parquet"
        if path.exists():
            parts.append(pd.read_parquet(path))
    if not parts:
        raise FileNotFoundError(symbol)
    frame = pd.concat(parts, ignore_index=True)
    frame = frame.sort_values("open_time").drop_duplicates("open_time", keep="last")
    frame["available_ms"] = pd.to_numeric(frame.close_time, errors="coerce").astype("int64") + 1
    frame["time"] = pd.to_datetime(frame.available_ms, unit="ms", utc=True).dt.floor("h")
    frame["symbol"] = symbol
    return frame.reset_index(drop=True)


def merge_metrics(symbol: str, metrics_dir: Path) -> pd.DataFrame:
    bars = load_bars(symbol)
    path = metrics_dir / f"{symbol}-metrics.parquet"
    if not path.exists():
        raise FileNotFoundError(path)
    metrics = pd.read_parquet(path).sort_values("available_ms")
    frame = pd.merge_asof(
        bars.sort_values("available_ms"),
        metrics,
        on="available_ms",
        direction="backward",
        tolerance=3_600_000,
    )
    frame["volume_ratio_24h"] = frame.quote_volume / (
        frame.quote_volume.shift(1).rolling(24, min_periods=12).median()
    ).replace(0.0, np.nan)
    return frame


def add_features(frame: pd.DataFrame, lookback: int) -> pd.DataFrame:
    result = frame.copy()
    result["price_move_pct"] = (result.close / result.close.shift(lookback) - 1.0) * 100.0
    result["oi_move_pct"] = (
        result.sum_open_interest / result.sum_open_interest.shift(lookback) - 1.0
    ) * 100.0
    return result


def signals(panel: pd.DataFrame, candidate: Candidate) -> pd.DataFrame:
    parts = [add_features(frame, candidate.lookback_hours) for _, frame in panel.groupby("symbol")]
    featured = pd.concat(parts, ignore_index=True)
    common = featured[
        featured.price_move_pct.abs().ge(candidate.minimum_price_move_pct)
        & featured.volume_ratio_24h.ge(candidate.minimum_volume_ratio)
    ].copy()
    if candidate.setup.startswith("flush"):
        common = common[common.oi_move_pct.le(-candidate.minimum_oi_move_pct)].copy()
    else:
        common = common[common.oi_move_pct.ge(candidate.minimum_oi_move_pct)].copy()
    shock_direction = np.sign(common.price_move_pct)
    if candidate.setup == "flush_reversal":
        common["direction"] = -shock_direction
    else:
        common["direction"] = shock_direction
    common = common[common.direction.ne(0)].copy()
    common["strength"] = (
        common.price_move_pct.abs() / candidate.minimum_price_move_pct
        + common.oi_move_pct.abs() / candidate.minimum_oi_move_pct
        + common.volume_ratio_24h / candidate.minimum_volume_ratio
    )
    return (
        common.sort_values(
            ["available_ms", "strength", "quote_volume"],
            ascending=[True, False, False],
        )
        .groupby("available_ms", sort=False)
        .head(1)
        .sort_values("available_ms")
    )


def simulate(signals_frame: pd.DataFrame, panel: pd.DataFrame, candidate: Candidate) -> pd.DataFrame:
    bars = {
        symbol: frame.sort_values("available_ms").reset_index(drop=True)
        for symbol, frame in panel.groupby("symbol")
    }
    trades: list[dict[str, Any]] = []
    free_at = -1
    for signal in signals_frame.itertuples(index=False):
        if int(signal.available_ms) < free_at:
            continue
        frame = bars[signal.symbol]
        indices = frame.index[frame.available_ms.eq(signal.available_ms)].tolist()
        if not indices or indices[0] + 1 >= len(frame):
            continue
        entry_index = indices[0] + 1
        entry = float(frame.loc[entry_index, "open"])
        direction = float(signal.direction)
        exit_index = min(entry_index + candidate.hold_hours - 1, len(frame) - 1)
        exit_price = float(frame.loc[exit_index, "close"])
        exit_reason = "timeout"
        for index in range(entry_index, min(entry_index + candidate.hold_hours, len(frame))):
            high = float(frame.loc[index, "high"])
            low = float(frame.loc[index, "low"])
            favorable = (high / entry - 1.0) * 100 if direction > 0 else (entry / low - 1.0) * 100
            adverse = (entry / low - 1.0) * 100 if direction > 0 else (high / entry - 1.0) * 100
            if adverse >= candidate.stop_pct:
                exit_price = entry * (1.0 - direction * candidate.stop_pct / 100.0)
                exit_index = index
                exit_reason = "stop"
                break
            if favorable >= candidate.target_pct:
                exit_price = entry * (1.0 + direction * candidate.target_pct / 100.0)
                exit_index = index
                exit_reason = "target"
                break
        gross_pct = direction * (exit_price / entry - 1.0) * 100.0
        trades.append(
            {
                "symbol": signal.symbol,
                "entry_time": frame.loc[entry_index, "time"],
                "exit_time": frame.loc[exit_index, "time"],
                "direction": int(direction),
                "gross_pct": gross_pct,
                "exit_reason": exit_reason,
                "price_move_pct": float(signal.price_move_pct),
                "oi_move_pct": float(signal.oi_move_pct),
                "volume_ratio_24h": float(signal.volume_ratio_24h),
            }
        )
        free_at = int(frame.loc[exit_index, "available_ms"]) + 1
    return pd.DataFrame(trades)


def metrics(trades: pd.DataFrame, cost_pct: float) -> dict[str, Any]:
    if trades.empty:
        return {"trades": 0, "profit_factor": 0.0, "net_pct_points": 0.0, "win_rate_pct": 0.0, "max_drawdown_pct_points": 0.0}
    net = trades.gross_pct - cost_pct
    wins = net.clip(lower=0.0).sum()
    losses = -net.clip(upper=0.0).sum()
    cumulative = net.cumsum()
    drawdown = cumulative - cumulative.cummax()
    return {
        "trades": int(len(trades)),
        "profit_factor": round(float(wins / losses), 6) if losses else (999.0 if wins else 0.0),
        "net_pct_points": round(float(net.sum()), 6),
        "win_rate_pct": round(float(net.gt(0.0).mean() * 100.0), 6),
        "mean_gross_bps": round(float(trades.gross_pct.mean() * 100.0), 6),
        "max_drawdown_pct_points": round(float(-drawdown.min()), 6),
    }


WINDOWS = {
    "development_2022_2023": ("2022-01-01", "2024-01-01"),
    "validation_2024": ("2024-01-01", "2025-01-01"),
    "test_2025": ("2025-01-01", "2026-01-01"),
    "final_blind_2026": ("2026-01-01", "2027-01-01"),
}


def evaluate(trades: pd.DataFrame) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for name, (start, end) in WINDOWS.items():
        scoped = trades[
            trades.entry_time.ge(start) & trades.entry_time.lt(end)
        ] if not trades.empty else trades
        report[name] = {
            "base": metrics(scoped, BASE_COST_PCT),
            "stress": metrics(scoped, STRESS_COST_PCT),
        }
    return report


def annual_stress(trades: pd.DataFrame) -> dict[str, dict[str, Any]]:
    if trades.empty:
        return {}
    years = pd.to_datetime(trades.entry_time, utc=True).dt.year
    return {
        str(year): metrics(trades.loc[years.eq(year)], STRESS_COST_PCT)
        for year in sorted(years.unique())
    }


def run(metrics_dir: Path, symbols: list[str]) -> dict[str, Any]:
    panel = pd.concat([merge_metrics(symbol, metrics_dir) for symbol in symbols], ignore_index=True)
    results: dict[str, Any] = {}
    frozen: list[str] = []
    accepted: list[str] = []
    for candidate in CANDIDATES:
        selected = signals(panel, candidate)
        trades = simulate(selected, panel, candidate)
        windows = evaluate(trades)
        annual = annual_stress(trades)
        development_years = [annual.get("2022", {}), annual.get("2023", {})]
        validation = windows["validation_2024"]["stress"]
        is_frozen = all(
            year.get("trades", 0) >= 20
            and year.get("profit_factor", 0.0) > 1.0
            and year.get("net_pct_points", 0.0) > 0.0
            for year in development_years
        )
        is_accepted = is_frozen and validation["trades"] >= 20 and validation["profit_factor"] > 1.0 and validation["net_pct_points"] > 0.0
        if is_frozen:
            frozen.append(candidate.name)
        if is_accepted:
            accepted.append(candidate.name)
        results[candidate.name] = {
            "candidate": asdict(candidate),
            "signals": int(len(selected)),
            "selected_trades": int(len(trades)),
            "windows": windows,
            "annual_stress": annual,
            "frozen_after_development": is_frozen,
            "accepted_after_validation": is_accepted,
        }
    return {
        "experiment": "s0_oi_flush",
        "method": "Pre-registered OI flush reversal/continuation and OI build-up breakout events; next-hour entry; conservative intrabar stop-first path; one non-overlapping S0 position.",
        "symbols": symbols,
        "costs_pct": {"base": BASE_COST_PCT, "stress": STRESS_COST_PCT},
        "frozen_candidates": frozen,
        "accepted_candidates": accepted,
        "decision": "research_candidate" if accepted else "rejected_not_positive_expectancy",
        "results": results,
        "limitations": ["Explicit funding cash flows are not included; stress cost is conservative but is not an exact substitute."],
    }


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = run(args.metrics, [symbol.upper() for symbol in args.symbols])
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("experiment", "frozen_candidates", "accepted_candidates", "decision")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
