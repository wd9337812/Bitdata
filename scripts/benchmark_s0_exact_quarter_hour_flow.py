from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data" / "research" / "binance_um_aggtrades_10s"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_exact_quarter_hour_flow"
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT", "DOGEUSDT", "ADAUSDT")
HORIZON_BINS = {240: 1_440, 480: 2_880, 720: 4_320}
THRESHOLDS = (0.0, 0.1, 0.2, 0.3)
BASE_ROUND_TRIP_COST = 0.0012
STRESS_ROUND_TRIP_COST = 0.0024


@dataclass(frozen=True)
class Variant:
    horizon_minutes: int
    imbalance_threshold: float

    @property
    def name(self) -> str:
        threshold = str(self.imbalance_threshold).replace(".", "p")
        return f"h{self.horizon_minutes}_oi{threshold}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Test exact first-10-second quarter-hour order imbalance with next-bin "
            "execution and frozen cost assumptions."
        )
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--development-end", default=None)
    return parser.parse_args()


def load_symbol(data: Path, symbol: str) -> pd.DataFrame:
    files = sorted((data / symbol).glob("*.parquet"))
    if not files:
        raise FileNotFoundError(data / symbol)
    frame = pd.concat([pd.read_parquet(path) for path in files], ignore_index=True)
    frame = frame.sort_values("bin_ms").drop_duplicates("bin_ms", keep="last")
    frame["time"] = pd.to_datetime(frame.bin_ms, unit="ms", utc=True)
    frame["symbol"] = symbol
    return frame.reset_index(drop=True)


def build_candidates(frame: pd.DataFrame) -> pd.DataFrame:
    indexed = frame.set_index("bin_ms")
    quarter = frame[
        frame.time.dt.minute.mod(15).eq(0) & frame.time.dt.second.eq(0)
    ].copy()
    quarter["entry_ms"] = quarter.bin_ms + 10_000
    quarter["entry_price"] = quarter.entry_ms.map(indexed.open)
    for horizon_minutes in HORIZON_BINS:
        exit_ms = quarter.entry_ms + horizon_minutes * 60_000
        quarter[f"exit_price_{horizon_minutes}"] = exit_ms.map(indexed.open)
        quarter[f"gross_forward_{horizon_minutes}"] = (
            quarter[f"exit_price_{horizon_minutes}"] / quarter.entry_price - 1.0
        )
    return quarter


def build_panel(data: Path, symbols: list[str]) -> pd.DataFrame:
    return pd.concat(
        [build_candidates(load_symbol(data, symbol)) for symbol in symbols],
        ignore_index=True,
    ).sort_values(["time", "symbol"])


def select_non_overlapping(panel: pd.DataFrame, variant: Variant) -> pd.DataFrame:
    forward = f"gross_forward_{variant.horizon_minutes}"
    eligible = panel[
        panel.order_imbalance.abs().ge(variant.imbalance_threshold)
        & panel.entry_price.notna()
        & panel[forward].notna()
    ].copy()
    if eligible.empty:
        return eligible
    strongest = eligible.loc[
        eligible.groupby("time").order_imbalance.apply(
            lambda values: values.abs().idxmax()
        )
    ].sort_values("time")
    selected: list[int] = []
    free_at: pd.Timestamp | None = None
    hold = pd.Timedelta(minutes=variant.horizon_minutes)
    for row in strongest.itertuples():
        if free_at is None or row.time >= free_at:
            selected.append(row.Index)
            free_at = row.time + pd.Timedelta(seconds=10) + hold
    trades = strongest.loc[selected].copy()
    trades["direction"] = np.sign(trades.order_imbalance).astype("int8")
    trades["gross_return"] = trades.direction * trades[forward]
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
        "win_rate_pct": round(float(net.gt(0.0).mean() * 100.0), 6),
        "profit_factor": round(float(wins / losses), 6)
        if losses
        else (999.0 if wins else 0.0),
        "net_pct_points": round(float(net.sum() * 100.0), 6),
        "mean_gross_bps": round(float(trades.gross_return.mean() * 10_000), 6),
        "mean_net_bps": round(float(net.mean() * 10_000), 6),
        "max_drawdown_pct_points": round(float(-drawdown.min() * 100.0), 6),
    }


def evaluate(trades: pd.DataFrame, development_end: str | None) -> dict[str, Any]:
    windows: dict[str, pd.DataFrame] = {"all": trades}
    if development_end:
        boundary = pd.Timestamp(development_end, tz="UTC")
        windows["development"] = trades[trades.time.lt(boundary)]
        windows["validation"] = trades[trades.time.ge(boundary)]
    return {
        name: {
            "base": trade_metrics(scoped, BASE_ROUND_TRIP_COST),
            "stress": trade_metrics(scoped, STRESS_ROUND_TRIP_COST),
        }
        for name, scoped in windows.items()
    }


def run(data: Path, symbols: list[str], development_end: str | None) -> dict[str, Any]:
    panel = build_panel(data, symbols)
    variants: dict[str, Any] = {}
    accepted: list[str] = []
    for horizon in HORIZON_BINS:
        for threshold in THRESHOLDS:
            variant = Variant(horizon, threshold)
            trades = select_non_overlapping(panel, variant)
            windows = evaluate(trades, development_end)
            required_windows = [windows["all"]]
            if development_end:
                required_windows.extend(
                    [windows["development"], windows["validation"]]
                )
            eligible = all(
                window["stress"]["trades"] >= 20
                and window["stress"]["profit_factor"] > 1.0
                and window["stress"]["net_pct_points"] > 0.0
                for window in required_windows
            )
            variants[variant.name] = {
                "variant": asdict(variant),
                "windows": windows,
                "accepted": eligible,
            }
            if eligible:
                accepted.append(variant.name)
    return {
        "experiment": "s0_exact_quarter_hour_flow",
        "paper_reference": "https://arxiv.org/abs/2607.09426",
        "method": (
            "Observe exact first 10 seconds after each quarter-hour boundary, enter "
            "at the first trade of the next 10-second bin, select the strongest "
            "absolute imbalance across symbols, and keep one non-overlapping position."
        ),
        "symbols": symbols,
        "panel_rows": int(len(panel)),
        "development_end": development_end,
        "costs": {
            "base_round_trip": BASE_ROUND_TRIP_COST,
            "stress_round_trip": STRESS_ROUND_TRIP_COST,
        },
        "limitations": [
            "Pilot excludes explicit funding; stress cost is intended to be conservative.",
            "Fixed-horizon results are a tradability screen, not a production strategy.",
        ],
        "variants": variants,
        "accepted_variants": accepted,
        "decision": "research_candidate" if accepted else "rejected_not_positive_expectancy",
    }


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = run(args.data, [symbol.upper() for symbol in args.symbols], args.development_end)
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "experiment": report["experiment"],
                "panel_rows": report["panel_rows"],
                "accepted_variants": report["accepted_variants"],
                "decision": report["decision"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
