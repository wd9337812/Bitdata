from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data" / "research" / "binance_um_quarter_hour_5m" / "parquet"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_quarter_hour_orderflow_5m"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT", "DOGEUSDT", "ADAUSDT")
PHASES = (0, 5, 10)
ROLLING_QUARTERS = 96 * 30
MIN_HISTORY_QUARTERS = 96 * 14
HOLD_BARS = 48
BASE_COST = 0.0012
STRESS_COST = 0.0024


def normalized_order_imbalance(buy_quote: pd.Series, quote_volume: pd.Series) -> pd.Series:
    total = pd.to_numeric(quote_volume, errors="coerce").replace(0.0, np.nan)
    buy = pd.to_numeric(buy_quote, errors="coerce")
    return (2.0 * buy / total - 1.0).clip(-1.0, 1.0)


def load_symbol(data: Path, symbol: str) -> pd.DataFrame:
    files = sorted((data / symbol).glob("*.parquet"))
    if not files:
        raise FileNotFoundError(data / symbol)
    columns = ("open_time", "open", "quote_volume", "taker_buy_quote_volume")
    frame = pd.concat(
        [pd.read_parquet(path, columns=columns) for path in files], ignore_index=True
    ).sort_values("open_time")
    frame = frame.drop_duplicates("open_time", keep="last").reset_index(drop=True)
    frame["time"] = pd.to_datetime(frame.open_time, unit="ms", utc=True)
    frame["order_imbalance"] = normalized_order_imbalance(
        frame.taker_buy_quote_volume, frame.quote_volume
    )
    # The signal bar must close before entry. Four hours is measured from that next open.
    frame["entry_price"] = frame.open.shift(-1)
    frame["exit_price"] = frame.open.shift(-(HOLD_BARS + 1))
    frame["gross_forward"] = frame.exit_price / frame.entry_price - 1.0
    frame["symbol"] = symbol
    return frame


def build_panel(data: Path, phase: int) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for symbol in SYMBOLS:
        frame = load_symbol(data, symbol)
        scoped = frame[frame.time.dt.minute.mod(15).eq(phase)].copy()
        history = scoped.order_imbalance.abs().shift(1).rolling(
            ROLLING_QUARTERS, min_periods=MIN_HISTORY_QUARTERS
        )
        scoped["historical_q95"] = history.quantile(0.95)
        frames.append(scoped)
    return pd.concat(frames, ignore_index=True).sort_values(["time", "symbol"])


def select_trades(panel: pd.DataFrame) -> pd.DataFrame:
    eligible = panel[
        panel.order_imbalance.abs().ge(panel.historical_q95)
        & panel.entry_price.notna()
        & panel.exit_price.notna()
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
    for row in strongest.itertuples():
        entry_time = row.time + pd.Timedelta(minutes=5)
        if free_at is None or entry_time >= free_at:
            selected.append(row.Index)
            free_at = entry_time + pd.Timedelta(hours=4)
    trades = strongest.loc[selected].copy()
    trades["direction"] = np.sign(trades.order_imbalance).astype("int8")
    trades["gross_return"] = trades.direction * trades.gross_forward
    return trades


def metrics(trades: pd.DataFrame, cost: float) -> dict[str, Any]:
    if trades.empty:
        return {
            "trades": 0,
            "win_rate_pct": 0.0,
            "profit_factor": 0.0,
            "net_return_sum": 0.0,
            "mean_net_bps": 0.0,
            "max_drawdown_return_sum": 0.0,
        }
    net = trades.gross_return - cost
    gains = net.clip(lower=0.0).sum()
    losses = -net.clip(upper=0.0).sum()
    equity = net.cumsum()
    drawdown = equity - equity.cummax()
    return {
        "trades": int(len(trades)),
        "win_rate_pct": round(float(net.gt(0.0).mean() * 100.0), 4),
        "profit_factor": round(float(gains / losses), 4)
        if losses
        else (999.0 if gains else 0.0),
        "net_return_sum": round(float(net.sum()), 6),
        "mean_net_bps": round(float(net.mean() * 10_000.0), 4),
        "max_drawdown_return_sum": round(float(-drawdown.min()), 6),
    }


def evaluate(trades: pd.DataFrame, cost: float) -> dict[str, Any]:
    result = metrics(trades, cost)
    net = trades.gross_return - cost
    top_winners = net.nlargest(min(3, len(net))).index
    result["without_top_3_winners"] = metrics(trades.drop(index=top_winners), cost)
    result["by_year"] = {
        str(year): metrics(group, cost)
        for year, group in trades.groupby(trades.time.dt.year)
    }
    return result


def run(data: Path) -> dict[str, Any]:
    phases: dict[str, Any] = {}
    for phase in PHASES:
        panel = build_panel(data, phase)
        trades = select_trades(panel)
        phases[str(phase)] = {
            "candidate_rows": int(
                panel.order_imbalance.abs().ge(panel.historical_q95).sum()
            ),
            "base": evaluate(trades, BASE_COST),
            "stress": evaluate(trades, STRESS_COST),
        }
    true_stress = phases["0"]["stress"]
    accepted = (
        true_stress["trades"] >= 200
        and true_stress["profit_factor"] > 1.0
        and true_stress["net_return_sum"] > 0.0
        and true_stress["without_top_3_winners"]["profit_factor"] > 1.0
        and all(
            year["profit_factor"] > 1.0 and year["net_return_sum"] > 0.0
            for year in true_stress["by_year"].values()
        )
    )
    return {
        "experiment": "s0_quarter_hour_orderflow_5m",
        "paper_reference": "https://arxiv.org/abs/2607.09426",
        "symbols": list(SYMBOLS),
        "method": (
            "At each 0/15/30/45 UTC five-minute bar, compare absolute taker order "
            "imbalance with its shifted trailing-30-day 95th percentile, enter at "
            "the next five-minute open, hold four hours, and allow one global position."
        ),
        "limitations": [
            "The paper uses the first 10 seconds; official 5m klines are a coarser proxy.",
            "The paper reports about 0.5 bp predictable return per boundary, below "
            "the frozen taker round-trip costs used here.",
        ],
        "costs": {"base": BASE_COST, "stress": STRESS_COST},
        "phases": phases,
        "accepted": accepted,
        "decision": "research_candidate" if accepted else "rejected_not_positive_expectancy",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit the quarter-hour order-flow effect.")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = run(args.data)
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"decision": report["decision"], "phases": report["phases"]}, indent=2))


if __name__ == "__main__":
    main()
