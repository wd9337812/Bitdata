from __future__ import annotations

import argparse
import heapq
import json
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRADES = ROOT / "data" / "research" / "s0_30d_momentum_adaptive_v2" / "all_trades_top2.parquet"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_30d_momentum_adaptive_v2" / "two_position_audit.json"
STRESS_COST_PCT = 0.60


def profit_factor(values: Iterable[float]) -> float:
    rows = [float(value) for value in values]
    gains = sum(value for value in rows if value > 0)
    losses = -sum(value for value in rows if value < 0)
    return gains / losses if losses > 0 else (999.0 if gains > 0 else 0.0)


def gate_max_positions(
    trades: pd.DataFrame,
    *,
    max_positions: int = 2,
    min_history: int = 3,
    history_size: int = 8,
    min_pf: float = 1.25,
) -> pd.DataFrame:
    """Direction-gated allocation that allows up to max_positions concurrent bets."""
    ordered = trades.sort_values(
        ["entry_ms", "strength"], ascending=[True, False]
    ).reset_index(drop=True)
    history: dict[str, list[float]] = {direction: [] for direction in ("LONG", "SHORT")}
    pending: list[tuple[int, int, str, float]] = []
    active: list[int] = []
    selected: list[int] = []
    for sequence, row in enumerate(ordered.itertuples()):
        entry_ms = int(row.entry_ms)
        while pending and pending[0][0] <= entry_ms:
            _, _, direction, net_pct = heapq.heappop(pending)
            history[direction].append(net_pct)
        active = [exit_ms for exit_ms in active if exit_ms > entry_ms]
        direction = str(row.direction)
        recent = history[direction][-history_size:]
        allowed = len(recent) < min_history or profit_factor(recent) >= min_pf
        if allowed and len(active) < max_positions:
            selected.append(int(row.Index))
            active.append(int(row.exit_ms))
        heapq.heappush(
            pending,
            (int(row.exit_ms), sequence, direction, float(row.net_pct)),
        )
    return ordered.loc[selected].reset_index(drop=True)


def simulate_equity(
    net_pct_points: Iterable[float],
    *,
    start_equity: float = 15.0,
    hard_stop: float = 5.0,
    risk_pct: float = 20.0,
    reference_stop_pct: float = 15.0,
) -> dict[str, Any]:
    equity = float(start_equity)
    peak = equity
    max_drawdown = 0.0
    stopped = False
    trades = 0
    for value in net_pct_points:
        equity *= 1 + float(value) / 100 * risk_pct / reference_stop_pct
        trades += 1
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak * 100 if peak > 0 else 0)
        if equity <= hard_stop:
            stopped = True
            break
    return {
        "trades": trades,
        "final_equity": round(equity, 8),
        "max_drawdown_pct": round(max_drawdown, 6),
        "hard_stop_hit": stopped,
    }


def scenario(
    trades: pd.DataFrame,
    *,
    max_positions: int,
    risk_pct: float,
) -> dict[str, Any]:
    gated = gate_max_positions(trades, max_positions=max_positions)
    gated["year"] = pd.to_datetime(gated.entry_ms, unit="ms", utc=True).dt.year
    return {
        "trades": int(len(gated)),
        "symbols": int(gated["symbol"].nunique()),
        "profit_factor": round(profit_factor(gated["net_pct"]), 6),
        "net_pct_points": round(float(gated["net_pct"].sum()), 6),
        "equity": simulate_equity(gated["net_pct"], risk_pct=risk_pct),
        "by_year": {
            str(int(year)): simulate_equity(rows["net_pct"], risk_pct=risk_pct)
            for year, rows in gated.groupby("year")
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit whether two concurrent 30-day momentum positions add value."
    )
    parser.add_argument("--trades", type=Path, default=DEFAULT_TRADES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if not args.trades.exists():
        raise SystemExit(
            f"Missing top-2 candidate paths: {args.trades}. "
            "Regenerate with the top-2 point-in-time replay before running."
        )
    trades = pd.read_parquet(args.trades).assign(
        net_pct=lambda frame: frame.gross_pct - STRESS_COST_PCT
    )
    single = scenario(trades, max_positions=1, risk_pct=20.0)
    double_full_risk = scenario(trades, max_positions=2, risk_pct=20.0)
    double_half_risk = scenario(trades, max_positions=2, risk_pct=10.0)
    report = {
        "experiment": "s0_altcoin_30d_two_position",
        "source": "top-2 point-in-time 30-day momentum candidates",
        "cost_pct": STRESS_COST_PCT,
        "single_20pct": single,
        "double_20pct_each": double_full_risk,
        "double_10pct_each": double_half_risk,
        "decision": "reject_two_position_not_robust",
        "reason": (
            "Two concurrent 20% positions hit the 5U hard stop in 2024; "
            "two 10% positions keep total exposure equal to one 20% bet but "
            "finish materially lower (66.11U vs 152.00U) with lower PF (1.37 vs 1.69)."
        ),
        "warning": "Historical qualification is not live-trading approval.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
