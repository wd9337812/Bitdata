from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
from typing import Any

from app.telemetry import list_strategy_runs


@dataclass
class TradeStats:
    trades: int
    win_rate: float
    avg_win_pct: float
    avg_loss_pct: float
    profit_factor: float
    fee_share_pct: float


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def infer_trade_stats(runs: list[dict[str, Any]]) -> TradeStats:
    closed: list[float] = []
    for run in runs:
        payload = run.get("payload") or {}
        result = payload.get("result") or {}
        decision = payload.get("decision") or {}
        pnl = _float(result.get("realized_pnl") or result.get("pnl") or decision.get("simulated_net_pct"), 0.0)
        if pnl:
            closed.append(pnl)
    if not closed:
        return TradeStats(trades=0, win_rate=0.46, avg_win_pct=1.15, avg_loss_pct=0.82, profit_factor=1.12, fee_share_pct=18.0)
    wins = [item for item in closed if item > 0]
    losses = [abs(item) for item in closed if item < 0]
    win_rate = len(wins) / len(closed) if closed else 0.0
    avg_win = mean(wins) if wins else 0.5
    avg_loss = mean(losses) if losses else 0.5
    pf = sum(wins) / max(sum(losses), 1e-9)
    return TradeStats(
        trades=len(closed),
        win_rate=win_rate,
        avg_win_pct=avg_win,
        avg_loss_pct=avg_loss,
        profit_factor=pf,
        fee_share_pct=18.0,
    )


def simulate_stage_path(
    config: dict[str, Any],
    *,
    start_equity: float | None = None,
    target_equity: float | None = None,
    days: int | None = None,
    trades_per_day: float | None = None,
    stats: TradeStats | None = None,
) -> dict[str, Any]:
    start = float(start_equity or config.get("simulation_start_equity", 50.0))
    target = float(target_equity or config.get("target_phase_a_equity", 10_000.0))
    days = int(days or config.get("target_phase_days", 30))
    mode = str(config.get("growth_mode", "balanced"))
    base_risk = float(config.get(f"{mode}_risk_per_trade_pct", config.get("risk_per_trade_pct", 1.0)) or 1.0)
    if mode == "extreme_sprint":
        base_risk = float(config.get("extreme_sprint_risk_per_trade_pct", base_risk))
    if mode == "tournament_sprint":
        base_risk = float(config.get("tournament_sprint_risk_per_trade_pct", base_risk))
    stats = stats or infer_trade_stats(list_strategy_runs(500))
    trades_per_day = float(trades_per_day or config.get("simulation_trades_per_day", 3.0))
    fee_pct = float(config.get("simulation_fee_slippage_pct", 0.12))
    equity = start
    high = start
    max_drawdown = 0.0
    equity_curve = []
    total_fees = 0.0
    total_trades = max(0, int(days * trades_per_day))
    for index in range(total_trades):
        is_win = (index % 100) / 100 < stats.win_rate
        gross_pct = stats.avg_win_pct if is_win else -stats.avg_loss_pct
        trade_risk_scale = base_risk / 10.0
        net_pct = gross_pct * trade_risk_scale - fee_pct
        fee_usdt = equity * fee_pct / 100
        total_fees += fee_usdt
        equity *= 1 + net_pct / 100
        high = max(high, equity)
        drawdown = (high - equity) / high * 100 if high > 0 else 0.0
        max_drawdown = max(max_drawdown, drawdown)
        if (index + 1) % max(1, int(trades_per_day)) == 0:
            equity_curve.append({"day": len(equity_curve) + 1, "equity": round(equity, 6)})
    required_daily = ((target / max(start, 1e-9)) ** (1 / max(days, 1)) - 1) * 100 if target > start else 0.0
    achieved = equity >= target
    return {
        "mode": mode,
        "start_equity": round(start, 6),
        "target_equity": round(target, 6),
        "days": days,
        "trades": total_trades,
        "trades_per_day": trades_per_day,
        "final_equity": round(equity, 6),
        "target_hit": achieved,
        "target_gap_pct": round((target - equity) / target * 100, 4) if target else 0.0,
        "required_daily_return_pct": round(required_daily, 4),
        "max_drawdown_pct": round(max_drawdown, 4),
        "win_rate_pct": round(stats.win_rate * 100, 4),
        "profit_factor": round(stats.profit_factor, 4),
        "fee_share_pct": round(total_fees / max(abs(equity - start) + total_fees, 1e-9) * 100, 4),
        "equity_curve": equity_curve,
        "warning": "\u8fd9\u662f\u53c2\u6570\u6bd4\u8f83\u6a21\u578b\uff0c\u4e0d\u662f\u76c8\u5229\u4fdd\u8bc1\u3002",
    }
