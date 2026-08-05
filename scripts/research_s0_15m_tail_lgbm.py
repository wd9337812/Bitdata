from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import lightgbm as lgb

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.research_s0_15m_mfe_baseline import fifteen_minute  # noqa: E402
from scripts.research_s0_phase1_highlev_replay import load_bars  # noqa: E402

DEFAULT_DATA = ROOT / "data" / "research" / "binance_um_1m_cross_year" / "parquet"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_15m_tail_lgbm"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT", "ADAUSDT", "BNBUSDT", "LINKUSDT")
FEATURES = ("ret_1", "ret_4", "ret_8", "vol_z", "taker_imbalance", "atr", "range_pct")
WINDOWS = {
    "development_2020_2023": ("2020-01-01", "2024-01-01"),
    "validation_2024": ("2024-01-01", "2025-01-01"),
    "test_2025": ("2025-01-01", "2026-01-01"),
    "final_blind_2026": ("2026-01-01", "2027-01-01"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Bounded 15m tail-MFE LightGBM recognizer: two binary models "
            "(long/short tail MFE>=2% within 8 bars), trained on 2020-2023, "
            "quantile selected on dev, frozen through 2024-2026."
        )
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--symbols", nargs="+", default=list(SYMBOLS))
    parser.add_argument("--dev-quantile", type=float, default=0.995)
    parser.add_argument("--stop-pct", type=float, default=1.0)
    parser.add_argument("--tp-r", type=float, default=3.0)
    parser.add_argument("--hold-bars", type=int, default=8)
    parser.add_argument("--cost-pct", type=float, default=0.24)
    parser.add_argument("--n-estimators", type=int, default=300)
    parser.add_argument("--seed", type=int, default=42)
    return parser.parse_args()


def add_labels_and_features(bars: pd.DataFrame) -> pd.DataFrame:
    out = bars.copy()
    out["ret_4"] = out.close.pct_change(4)
    out["ret_8"] = out.close.pct_change(8)
    out["range_pct"] = (out.high - out.low) / out.close
    close = out.close.to_numpy()
    high = out.high.to_numpy()
    low = out.low.to_numpy()
    n = len(out)
    mfe_long = np.full(n, np.nan)
    mfe_short = np.full(n, np.nan)
    for offset in range(1, 9):
        if offset >= n:
            break
        long_vals = high[offset:] / close[:-offset] - 1.0
        short_vals = close[:-offset] / low[offset:] - 1.0
        current_long = mfe_long[:-offset]
        current_short = mfe_short[:-offset]
        mfe_long[:-offset] = np.maximum(
            np.nan_to_num(current_long, nan=-1.0), long_vals
        )
        mfe_short[:-offset] = np.maximum(
            np.nan_to_num(current_short, nan=-1.0), short_vals
        )
    out["mfe_long_pct"] = mfe_long * 100.0
    out["mfe_short_pct"] = mfe_short * 100.0
    out["label_long"] = mfe_long >= 0.02
    out["label_short"] = mfe_short >= 0.02
    return out.iloc[:-8].reset_index(drop=True)


def build_panel(data: Path, symbols: list[str]) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for symbol in symbols:
        path = data / f"{symbol}.parquet"
        if not path.exists():
            continue
        frame = load_bars(path)
        if frame is None or frame.empty:
            continue
        bars = add_labels_and_features(fifteen_minute(frame))
        if len(bars) < 200:
            continue
        bars["symbol"] = symbol
        parts.append(bars)
    if not parts:
        raise SystemExit("no panel")
    return pd.concat(parts, ignore_index=True).dropna(subset=FEATURES).reset_index(drop=True)


def simulate_candidates(
    candidates: pd.DataFrame,
    bars_by_symbol: dict[str, pd.DataFrame],
    args: argparse.Namespace,
) -> pd.DataFrame:
    ordered = candidates.sort_values("open_time").reset_index(drop=True)
    trades: list[dict[str, Any]] = []
    busy_until = -1
    for row in ordered.itertuples(index=False):
        bars = bars_by_symbol[row.symbol]
        entry_ms = int(row.open_time) + 900_000
        if entry_ms < busy_until:
            continue
        start = int(bars.open_time.searchsorted(entry_ms, side="left"))
        path = bars.iloc[start : start + args.hold_bars]
        if path.empty or int(path.iloc[0].open_time) != entry_ms:
            continue
        entry = float(path.iloc[0].open)
        stop = entry - row.direction * entry * args.stop_pct / 100.0
        target = entry + row.direction * entry * args.stop_pct * args.tp_r / 100.0
        exit_price = float(path.iloc[-1].close)
        outcome = "TIME"
        exit_ms = int(path.iloc[-1].open_time) + 900_000
        for bar in path.itertuples(index=False):
            if row.direction > 0 and float(bar.low) <= stop:
                exit_price, outcome = stop, "STOP"
            elif row.direction < 0 and float(bar.high) >= stop:
                exit_price, outcome = stop, "STOP"
            elif row.direction > 0 and float(bar.high) >= target:
                exit_price, outcome = target, "TARGET"
            elif row.direction < 0 and float(bar.low) <= target:
                exit_price, outcome = target, "TARGET"
            else:
                continue
            exit_ms = int(bar.open_time) + 900_000
            break
        gross_pct = row.direction * (exit_price / entry - 1.0) * 100.0
        trades.append(
            {
                "symbol": row.symbol,
                "entry_ms": entry_ms,
                "exit_ms": exit_ms,
                "direction": int(row.direction),
                "gross_pct": gross_pct,
                "outcome": outcome,
            }
        )
        busy_until = exit_ms
    return pd.DataFrame(trades)


def metrics(trades: pd.DataFrame, cost_pct: float) -> dict[str, Any]:
    if trades.empty:
        return {"trades": 0}
    net = trades.gross_pct - cost_pct
    wins = net.clip(lower=0).sum()
    losses = -net.clip(upper=0).sum()
    return {
        "trades": int(len(trades)),
        "win_rate_pct": round(float((net > 0).mean() * 100.0), 2),
        "profit_factor": round(float(wins / losses), 3) if losses > 0 else 999.0,
        "net_sum_pct": round(float(net.sum()), 3),
    }


def main() -> None:
    args = parse_args()
    panel = build_panel(args.data, args.symbols)
    dev_mask = panel.open_time.lt(pd.Timestamp("2024-01-01", tz="UTC").timestamp() * 1000)
    dev = panel.loc[dev_mask].copy()
    params = {
        "objective": "binary",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "min_data_in_leaf": 500,
        "n_estimators": args.n_estimators,
        "random_state": args.seed,
        "verbose": -1,
    }
    models: dict[str, Any] = {}
    for side, label in (("long", "label_long"), ("short", "label_short")):
        positive = int(dev[label].sum())
        negative = int((~dev[label]).sum())
        model = lgb.LGBMClassifier(
            **params,
            scale_pos_weight=negative / positive if positive else 1.0,
        )
        model.fit(dev[list(FEATURES)], dev[label])
        models[side] = model
    panel = panel.copy()
    panel["p_long"] = models["long"].predict_proba(panel[list(FEATURES)])[:, 1]
    panel["p_short"] = models["short"].predict_proba(panel[list(FEATURES)])[:, 1]
    panel["score"] = panel[["p_long", "p_short"]].max(axis=1)
    panel["direction"] = np.where(panel.p_long >= panel.p_short, 1, -1)
    threshold = float(panel.loc[dev_mask, "score"].quantile(args.dev_quantile))
    candidates = panel.loc[panel.score.ge(threshold)].copy()
    bars_by_symbol = {
        symbol: fifteen_minute(load_bars(args.data / f"{symbol}.parquet"))
        for symbol in args.symbols
        if (args.data / f"{symbol}.parquet").exists()
    }
    trades = simulate_candidates(candidates, bars_by_symbol, args)
    args.output.mkdir(parents=True, exist_ok=True)
    trades.to_parquet(args.output / "trades.parquet", index=False)
    report: dict[str, Any] = {
        "experiment": "s0_15m_tail_lgbm",
        "rows": int(len(panel)),
        "dev_tail_rate": round(float(dev.label_long.mean() + dev.label_short.mean()), 5),
        "threshold": round(threshold, 6),
        "candidates": int(len(candidates)),
        "windows": {},
        "annual": {},
    }
    if not trades.empty:
        year = pd.to_datetime(trades.entry_ms, unit="ms", utc=True).dt.year
        trades["year"] = year
        for name, (start, end) in WINDOWS.items():
            mask = pd.to_datetime(trades.entry_ms, unit="ms", utc=True).between(
                pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
            )
            report["windows"][name] = metrics(trades.loc[mask], args.cost_pct)
        for value in sorted(trades.year.unique()):
            report["annual"][str(value)] = metrics(
                trades.loc[trades.year.eq(value)], args.cost_pct
            )
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
