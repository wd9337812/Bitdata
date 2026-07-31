from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data" / "research" / "s0_public_1m" / "parquet"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_quarter_hour_order_flow"
SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "XRPUSDT",
    "SOLUSDT",
    "DOGEUSDT",
    "ADAUSDT",
)
HORIZONS_MINUTES = (240, 480, 720)
ZSCORE_THRESHOLDS = (0.0, 1.0, 1.5, 2.0, 2.5)
CONFIRMATIONS = ("none", "previous_quarter", "previous_hour")
BASE_ROUND_TRIP_COST = 0.0012
STRESS_ROUND_TRIP_COST = 0.0024
ROLLING_QUARTERS = 96 * 30
MIN_HISTORY_QUARTERS = 96 * 14


@dataclass(frozen=True)
class Variant:
    horizon_minutes: int
    zscore_threshold: float
    confirmation: str

    @property
    def name(self) -> str:
        threshold = str(self.zscore_threshold).replace(".", "p")
        return f"h{self.horizon_minutes}_z{threshold}_{self.confirmation}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Test an executable one-minute proxy for the published quarter-hour "
            "opening order-flow effect on six liquid Binance perpetuals."
        )
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def normalized_order_imbalance(
    taker_buy_quote: pd.Series,
    quote_volume: pd.Series,
) -> pd.Series:
    total = pd.to_numeric(quote_volume, errors="coerce").replace(0.0, np.nan)
    buy = pd.to_numeric(taker_buy_quote, errors="coerce")
    return (2.0 * buy / total - 1.0).clip(-1.0, 1.0)


def add_historical_zscore(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    history = result.order_imbalance.shift(1).rolling(
        ROLLING_QUARTERS,
        min_periods=MIN_HISTORY_QUARTERS,
    )
    scale = history.std(ddof=0).replace(0.0, np.nan)
    result["oi_zscore"] = (result.order_imbalance - history.mean()) / scale
    result["previous_quarter"] = np.sign(result.order_imbalance).eq(
        np.sign(result.order_imbalance.shift(1))
    )
    result["previous_hour"] = np.sign(result.order_imbalance).eq(
        np.sign(result.order_imbalance.shift(4))
    )
    return result


def load_symbol(path: Path, symbol: str) -> pd.DataFrame:
    columns = [
        "open_time",
        "open",
        "close",
        "quote_volume",
        "taker_buy_quote",
    ]
    minute = pd.read_parquet(path, columns=columns).sort_values("open_time")
    minute = minute.reset_index(drop=True)
    minute["time"] = pd.to_datetime(minute.open_time, unit="ms", utc=True)
    minute["order_imbalance"] = normalized_order_imbalance(
        minute.taker_buy_quote,
        minute.quote_volume,
    )
    minute["entry_price"] = minute.open.shift(-1)
    for horizon in HORIZONS_MINUTES:
        minute[f"gross_forward_{horizon}"] = (
            minute.close.shift(-horizon) / minute.entry_price - 1.0
        )
    quarter = minute[minute.time.dt.minute.mod(15).eq(0)].copy()
    quarter = add_historical_zscore(quarter)
    quarter["symbol"] = symbol
    return quarter


def build_panel(data: Path) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for symbol in SYMBOLS:
        path = data / f"{symbol}.parquet"
        if not path.exists():
            raise FileNotFoundError(path)
        frames.append(load_symbol(path, symbol))
    return pd.concat(frames, ignore_index=True).sort_values(["time", "symbol"])


def select_non_overlapping(panel: pd.DataFrame, variant: Variant) -> pd.DataFrame:
    candidates = panel[
        panel.oi_zscore.abs().ge(variant.zscore_threshold)
        & panel[f"gross_forward_{variant.horizon_minutes}"].notna()
    ].copy()
    if variant.confirmation != "none":
        candidates = candidates[candidates[variant.confirmation]]
    if candidates.empty:
        return candidates

    strongest = candidates.loc[
        candidates.groupby("time").oi_zscore.apply(lambda values: values.abs().idxmax())
    ].sort_values("time")
    selected: list[int] = []
    free_at: pd.Timestamp | None = None
    hold = pd.Timedelta(minutes=variant.horizon_minutes)
    for row in strongest.itertuples():
        if free_at is None or row.time >= free_at:
            selected.append(row.Index)
            free_at = row.time + hold
    trades = strongest.loc[selected].copy()
    trades["direction"] = np.sign(trades.order_imbalance).astype("int8")
    trades["gross_return"] = (
        trades.direction * trades[f"gross_forward_{variant.horizon_minutes}"]
    )
    return trades


def trade_metrics(trades: pd.DataFrame, cost: float) -> dict[str, Any]:
    if trades.empty:
        return {
            "trades": 0,
            "symbols": 0,
            "win_rate_pct": 0.0,
            "profit_factor": 0.0,
            "net_pct_points": 0.0,
            "mean_gross_bps": 0.0,
            "mean_net_bps": 0.0,
            "max_drawdown_pct_points": 0.0,
        }
    net = trades.gross_return - cost
    wins = net.clip(lower=0.0).sum()
    losses = -net.clip(upper=0.0).sum()
    cumulative = net.cumsum()
    drawdown = cumulative - cumulative.cummax()
    return {
        "trades": int(len(trades)),
        "symbols": int(trades.symbol.nunique()),
        "win_rate_pct": round(float(net.gt(0).mean() * 100.0), 6),
        "profit_factor": round(float(wins / losses), 6)
        if losses
        else (999.0 if wins else 0.0),
        "net_pct_points": round(float(net.sum() * 100.0), 6),
        "mean_gross_bps": round(float(trades.gross_return.mean() * 10_000), 6),
        "mean_net_bps": round(float(net.mean() * 10_000), 6),
        "max_drawdown_pct_points": round(float(-drawdown.min() * 100.0), 6),
    }


WINDOWS = {
    "development_jan_mar": ("2026-01-01", "2026-04-01"),
    "validation_apr_may": ("2026-04-01", "2026-06-01"),
    "test_jun_jul": ("2026-06-01", "2026-08-01"),
}


def evaluate_variant(trades: pd.DataFrame) -> dict[str, Any]:
    windows: dict[str, Any] = {}
    for name, (start, end) in WINDOWS.items():
        scoped = trades[trades.time.ge(start) & trades.time.lt(end)]
        windows[name] = {
            "base": trade_metrics(scoped, BASE_ROUND_TRIP_COST),
            "stress": trade_metrics(scoped, STRESS_ROUND_TRIP_COST),
        }
    return windows


def qualifies(windows: dict[str, Any]) -> bool:
    for window in ("validation_apr_may", "test_jun_jul"):
        for cost_case in ("base", "stress"):
            metrics = windows[window][cost_case]
            if (
                metrics["trades"] < 20
                or metrics["profit_factor"] <= 1.0
                or metrics["net_pct_points"] <= 0.0
            ):
                return False
    return True


def run(data: Path) -> dict[str, Any]:
    panel = build_panel(data)
    results: dict[str, Any] = {}
    accepted: list[str] = []
    for horizon in HORIZONS_MINUTES:
        for threshold in ZSCORE_THRESHOLDS:
            for confirmation in CONFIRMATIONS:
                variant = Variant(horizon, threshold, confirmation)
                trades = select_non_overlapping(panel, variant)
                windows = evaluate_variant(trades)
                eligible = qualifies(windows)
                results[variant.name] = {
                    "variant": asdict(variant),
                    "selected_trades": int(len(trades)),
                    "windows": windows,
                    "accepted": eligible,
                }
                if eligible:
                    accepted.append(variant.name)
    return {
        "experiment": "s0_quarter_hour_order_flow",
        "source": str(data),
        "paper_reference": "https://arxiv.org/abs/2607.09426",
        "method": (
            "Executable one-minute proxy: use the completed quarter-hour opening "
            "minute's normalized taker order imbalance, enter at the next minute, "
            "rank six liquid contracts, and allow only one non-overlapping position."
        ),
        "limitation": (
            "The source paper uses the first 10 seconds of aggregate trades. One-minute "
            "klines cannot reproduce trade-size roundness or the exact 10-second window."
        ),
        "symbols": list(SYMBOLS),
        "panel_rows": int(len(panel)),
        "costs": {
            "base_round_trip": BASE_ROUND_TRIP_COST,
            "stress_round_trip": STRESS_ROUND_TRIP_COST,
        },
        "variants": results,
        "accepted_variants": accepted,
        "decision": "research_candidate" if accepted else "rejected_not_positive_expectancy",
    }


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = run(args.data)
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    summary = {
        "experiment": report["experiment"],
        "panel_rows": report["panel_rows"],
        "accepted_variants": report["accepted_variants"],
        "decision": report["decision"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
