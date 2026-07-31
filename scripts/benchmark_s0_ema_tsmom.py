from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_s0_donchian_ensemble import (  # noqa: E402
    ONE_WAY_COST,
    daily_symbol_frame,
    return_metrics,
)
from scripts.benchmark_s0_donchian_long_short import extract_trades  # noqa: E402
from scripts.benchmark_s0_xmom_point_in_time import load_manifest  # noqa: E402


DEFAULT_DATA = ROOT / "data" / "research" / "binance_um_point_in_time_1h_2024_2025"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_ema_tsmom"
SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "DOGEUSDT",
    "SOLUSDT",
    "LTCUSDT",
)
EMA_PAIRS = ((8, 24), (16, 48), (32, 96))
NORMALIZATION_DAYS = 30
WARMUP_DAYS = 223


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reproduce a cost-aware multi-horizon EMA crypto TSMOM baseline."
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def paper_ema(series: pd.Series, length: int) -> pd.Series:
    """EMA using the paper's decay lambda=exp(-1/L)."""
    alpha = 1.0 - np.exp(-1.0 / length)
    return series.ewm(alpha=alpha, adjust=False, min_periods=length).mean()


def composite_signals(close: pd.Series) -> pd.DataFrame:
    components: list[pd.Series] = []
    for short, long in EMA_PAIRS:
        spread = paper_ema(close, short) - paper_ema(close, long)
        scale = spread.rolling(
            NORMALIZATION_DAYS, min_periods=NORMALIZATION_DAYS
        ).std(ddof=0)
        normalized = spread / scale.replace(0.0, np.nan)
        smoothed = paper_ema(normalized, long)
        components.append(np.tanh(smoothed))
    bounded = pd.concat(components, axis=1).mean(axis=1)
    composite_scale = bounded.rolling(
        NORMALIZATION_DAYS, min_periods=NORMALIZATION_DAYS
    ).std(ddof=0)
    standardized = bounded / composite_scale.replace(0.0, np.nan)
    return pd.DataFrame({"bounded": bounded, "standardized": standardized})


def composite_signal(close: pd.Series) -> pd.Series:
    return composite_signals(close).standardized


def build_panel(data: Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    starts, manifest = load_manifest(data)
    frames: list[pd.DataFrame] = []
    for symbol in SYMBOLS:
        path = data / "parquet" / f"{symbol}.parquet"
        frame = daily_symbol_frame(path, starts[symbol]).copy()
        signals = composite_signals(frame.close)
        frame["bounded_signal"] = signals.bounded
        frame["signal"] = signals.standardized
        frame["available_days"] = np.arange(1, len(frame) + 1)
        frames.append(frame)
    return pd.concat(frames, ignore_index=True), manifest


def portfolio_daily(
    panel: pd.DataFrame,
    position_model: str = "sign",
) -> pd.DataFrame:
    frame = panel.copy()
    if position_model == "sign":
        frame["position"] = np.sign(frame.signal.fillna(0.0)) / len(SYMBOLS)
    elif position_model == "bounded":
        frame["position"] = frame.bounded_signal.fillna(0.0) / len(SYMBOLS)
    elif position_model == "standardized_clipped":
        frame["position"] = frame.signal.clip(-1.0, 1.0).fillna(0.0) / len(SYMBOLS)
    else:
        raise ValueError(f"unknown position model: {position_model}")
    frame.loc[frame.available_days.lt(WARMUP_DAYS), "position"] = 0.0
    frame["previous_position"] = frame.groupby("symbol").position.shift().fillna(0)
    frame["turnover"] = (frame.position - frame.previous_position).abs()
    frame["asset_gross"] = frame.position * frame.next_return.fillna(0.0)
    frame["asset_net"] = (
        frame.asset_gross - frame.turnover * ONE_WAY_COST
    )
    daily = frame.groupby("day", as_index=False).agg(
        gross_return=("asset_gross", "sum"),
        net_return=("asset_net", "sum"),
        turnover=("turnover", "sum"),
    )
    return daily


def single_position_daily(panel: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    previous_symbol: str | None = None
    previous_direction = 0
    for day, scoped in panel.groupby("day", sort=True):
        eligible = scoped[
            scoped.signal.notna() & scoped.available_days.ge(WARMUP_DAYS)
        ]
        if eligible.empty:
            symbol = None
            direction = 0
            gross = 0.0
        else:
            selected = eligible.loc[eligible.signal.abs().idxmax()]
            symbol = str(selected.symbol)
            direction = int(np.sign(selected.signal))
            gross = direction * float(selected.next_return or 0.0)
        turnover = 0.0
        if previous_direction:
            turnover += 1.0
        if direction:
            turnover += 1.0
        if symbol == previous_symbol and direction == previous_direction:
            turnover = 0.0
        rows.append(
            {
                "day": day,
                "symbol": symbol,
                "direction": direction,
                "gross_return": gross,
                "turnover": turnover,
                "net_return": gross - turnover * ONE_WAY_COST,
            }
        )
        previous_symbol = symbol
        previous_direction = direction
    return pd.DataFrame(rows)


def window_report(daily: pd.DataFrame, start: str, end: str) -> dict[str, Any]:
    scoped = daily[daily.day.ge(start) & daily.day.lt(end)]
    trades = extract_trades(scoped) if "direction" in scoped else pd.DataFrame()
    if trades.empty:
        trade_metrics = {"trades": 0, "win_rate_pct": 0.0, "profit_factor": 0.0}
    else:
        winners = trades.net_return.clip(lower=0).sum()
        losers = -trades.net_return.clip(upper=0).sum()
        trade_metrics = {
            "trades": int(len(trades)),
            "win_rate_pct": round(float(trades.net_return.gt(0).mean() * 100), 6),
            "profit_factor": round(float(winners / losers), 6)
            if losers
            else (999.0 if winners else 0.0),
        }
    return {"daily": return_metrics(scoped), "trades": trade_metrics}


def evaluate(daily: pd.DataFrame) -> dict[str, Any]:
    return {
        "development_2024h2": window_report(daily, "2024-07-01", "2025-01-01"),
        "validation_2025h1": window_report(daily, "2025-01-01", "2025-07-01"),
        "test_2025h2": window_report(daily, "2025-07-01", "2026-01-01"),
    }


def main() -> None:
    args = parse_args()
    panel, manifest = build_panel(args.data)
    candidates = {
        "paper_sign_equal_weight_portfolio": evaluate(
            portfolio_daily(panel, "sign")
        ),
        "paper_continuous_bounded_portfolio": evaluate(
            portfolio_daily(panel, "bounded")
        ),
        "paper_standardized_clipped_portfolio": evaluate(
            portfolio_daily(panel, "standardized_clipped")
        ),
        "s0_strongest_signal_single_position": evaluate(single_position_daily(panel)),
    }
    qualified = [
        name
        for name, windows in candidates.items()
        if all(
            windows[window]["daily"].get("total_return_pct", 0) > 0
            for window in windows
        )
    ]
    result = {
        "experiment": "cost_aware_multi_horizon_ema_tsmom",
        "source": manifest.get("source"),
        "paper": "Gbadebo (2026), doi:10.15388/batp.2026.1",
        "paper_cost_caveat": "Original study excludes fees, slippage, and funding.",
        "paper_reproducibility_caveat": (
            "The paper alternately describes sign positions and signal-proportional "
            "positions, while its re-standardized signal is not necessarily bounded. "
            "All non-tuned plausible interpretations are reported."
        ),
        "symbols": list(SYMBOLS),
        "ema_pairs": [list(pair) for pair in EMA_PAIRS],
        "one_way_cost_pct": ONE_WAY_COST * 100,
        "candidates": candidates,
        "qualified_candidates": qualified,
        "decision": "proceed_to_2026" if qualified else "research_only_not_eligible",
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
