from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_s0_market_tsmom_28d import (
    DEFAULT_DATA,
    STRESS_ONE_WAY_COST,
    build_daily_panel,
    market_state,
)
from scripts.benchmark_s0_market_tsmom_consensus import consensus_state
from scripts.benchmark_s0_market_tsmom_trailing import Variant, simulate


OUTPUT = ROOT / "data" / "research" / "s0_market_tsmom_executable"
VARIANT = Variant(10, 3.0, 0.15, 20)
SYMBOL_RULES = {
    "BTCUSDT": {"min_qty": 0.001, "qty_step": 0.001},
    "ETHUSDT": {"min_qty": 0.001, "qty_step": 0.001},
}


def asset_daily(panel: pd.DataFrame, symbol: str) -> pd.DataFrame:
    frame = panel.loc[
        panel.symbol.eq(symbol), ["day", "open", "close"]
    ].drop_duplicates("day", keep="last")
    high_low_parts = []
    for root in DEFAULT_DATA:
        path = root / "parquet" / f"{symbol}.parquet"
        if not path.exists():
            continue
        hourly = pd.read_parquet(path, columns=["open_time", "high", "low"])
        hourly["day"] = pd.to_datetime(
            hourly.open_time, unit="ms", utc=True
        ).dt.floor("D")
        high_low_parts.append(
            hourly.groupby("day", as_index=False).agg(
                high=("high", "max"), low=("low", "min")
            )
        )
    high_low = (
        pd.concat(high_low_parts, ignore_index=True)
        .drop_duplicates("day", keep="last")
        .sort_values("day")
    )
    frame = frame.merge(high_low, on="day", how="inner").sort_values("day")
    previous_close = frame.close.shift(1)
    frame["true_range"] = pd.concat(
        [
            frame.high - frame.low,
            (frame.high - previous_close).abs(),
            (frame.low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    frame["atr_10"] = frame.true_range.rolling(10, min_periods=10).mean()
    return frame.reset_index(drop=True)


def _floor_step(value: float, step: float) -> float:
    if value <= 0 or step <= 0:
        return 0.0
    return math.floor((value + 1e-12) / step) * step


def executable_metrics(
    trades_by_symbol: dict[str, pd.DataFrame],
    *,
    preferred_symbol: str,
    fallback_symbol: str | None,
    starting_equity: float,
    risk_pct: float,
    max_leverage: float,
    margin_pct: float = 0.90,
    hard_stop: float = 5.0,
    reserve: float = 0.50,
) -> dict[str, Any]:
    preferred = trades_by_symbol[preferred_symbol].copy()
    fallback = (
        trades_by_symbol[fallback_symbol].copy()
        if fallback_symbol
        else pd.DataFrame()
    )
    fallback_by_day = (
        {row.entry_day: row for row in fallback.itertuples(index=False)}
        if not fallback.empty
        else {}
    )
    equity = float(starting_equity)
    peak = equity
    max_drawdown = 0.0
    rows: list[dict[str, Any]] = []
    for preferred_trade in preferred.itertuples(index=False):
        chosen = preferred_trade
        chosen_symbol = preferred_symbol
        rules = SYMBOL_RULES[chosen_symbol]

        def size(trade: Any, symbol: str) -> tuple[float, float, float]:
            entry = float(trade.entry_price)
            stop_pct = max(1e-9, float(trade.initial_stop_pct))
            risk_budget = min(
                equity * risk_pct,
                max(0.0, equity - hard_stop - reserve),
            )
            target_notional = risk_budget / stop_pct
            margin_notional = equity * margin_pct * max_leverage
            raw_qty = min(target_notional, margin_notional) / entry
            symbol_rules = SYMBOL_RULES[symbol]
            qty = _floor_step(raw_qty, symbol_rules["qty_step"])
            if qty < symbol_rules["min_qty"]:
                return 0.0, 0.0, 0.0
            notional = qty * entry
            actual_risk = notional * stop_pct
            return qty, notional, actual_risk

        qty, notional, actual_risk = size(chosen, chosen_symbol)
        if qty <= 0 and fallback_symbol:
            fallback_trade = fallback_by_day.get(preferred_trade.entry_day)
            if fallback_trade is not None:
                chosen = fallback_trade
                chosen_symbol = fallback_symbol
                rules = SYMBOL_RULES[chosen_symbol]
                qty, notional, actual_risk = size(chosen, chosen_symbol)
        if qty <= 0:
            rows.append(
                {
                    "entry_day": preferred_trade.entry_day,
                    "symbol": preferred_symbol,
                    "status": "skipped_min_contract",
                    "equity": equity,
                }
            )
            continue
        net_pnl = notional * float(chosen.net_return)
        equity += net_pnl
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1.0)
        rows.append(
            {
                "entry_day": chosen.entry_day,
                "symbol": chosen_symbol,
                "status": "executed",
                "quantity": qty,
                "notional": notional,
                "risk_usdt": actual_risk,
                "risk_pct": actual_risk / max(equity - net_pnl, 1e-9),
                "net_pnl": net_pnl,
                "equity": equity,
            }
        )
        if equity <= hard_stop:
            break
    executed = [row for row in rows if row["status"] == "executed"]
    wins = sum(max(0.0, float(row["net_pnl"])) for row in executed)
    losses = sum(max(0.0, -float(row["net_pnl"])) for row in executed)
    return {
        "starting_equity": starting_equity,
        "ending_equity": round(equity, 6),
        "net_return_pct": round((equity / starting_equity - 1.0) * 100, 4),
        "executed": len(executed),
        "skipped_min_contract": sum(
            row["status"] == "skipped_min_contract" for row in rows
        ),
        "win_rate_pct": round(
            sum(float(row["net_pnl"]) > 0 for row in executed)
            / max(1, len(executed))
            * 100,
            4,
        ),
        "profit_factor": round(wins / losses, 4) if losses else 999.0,
        "max_drawdown_pct": round(max_drawdown * 100, 4),
        "hard_stopped": equity <= hard_stop,
        "symbols": {
            symbol: sum(row.get("symbol") == symbol for row in executed)
            for symbol in SYMBOL_RULES
        },
        "stress_one_way_cost_pct": STRESS_ONE_WAY_COST * 100,
    }


def main() -> None:
    panel = build_daily_panel(DEFAULT_DATA)
    state = consensus_state(market_state(panel), 56)
    trades = {
        symbol: simulate(asset_daily(panel, symbol), state, VARIANT)
        for symbol in SYMBOL_RULES
    }
    oos = {
        symbol: frame.loc[
            pd.to_datetime(frame.entry_day, utc=True).dt.year.ge(2024)
        ].reset_index(drop=True)
        for symbol, frame in trades.items()
    }
    scenarios = {}
    for risk_pct in (0.10, 0.30):
        for max_leverage in (2.0, 3.0, 5.0):
            key = f"risk{int(risk_pct * 100)}_lev{int(max_leverage)}"
            scenarios[f"btc_{key}"] = executable_metrics(
                oos,
                preferred_symbol="BTCUSDT",
                fallback_symbol=None,
                starting_equity=15.153,
                risk_pct=risk_pct,
                max_leverage=max_leverage,
            )
            scenarios[f"btc_eth_fallback_{key}"] = executable_metrics(
                oos,
                preferred_symbol="BTCUSDT",
                fallback_symbol="ETHUSDT",
                starting_equity=15.153,
                risk_pct=risk_pct,
                max_leverage=max_leverage,
            )
    preferred = "BTCUSDT"
    fallback = "ETHUSDT"
    validation: dict[str, Any] = {}
    periods = {
        "train_2020_2023": (2020, 2023),
        "oos_2024_2026": (2024, 2026),
        **{str(year): (year, year) for year in range(2020, 2027)},
    }
    for name, (start_year, end_year) in periods.items():
        sliced = {
            symbol: frame.loc[
                pd.to_datetime(frame.entry_day, utc=True).dt.year.between(
                    start_year, end_year
                )
            ].reset_index(drop=True)
            for symbol, frame in trades.items()
        }
        validation[name] = executable_metrics(
            sliced,
            preferred_symbol=preferred,
            fallback_symbol=fallback,
            starting_equity=15.153,
            risk_pct=0.10,
            max_leverage=2.0,
        )
    delay_validation = {}
    for delay in (0, 1, 2):
        delayed = {
            symbol: simulate(
                asset_daily(panel, symbol),
                state,
                VARIANT,
                execution_delay_days=delay,
            )
            for symbol in SYMBOL_RULES
        }
        delayed_oos = {
            symbol: frame.loc[
                pd.to_datetime(frame.entry_day, utc=True).dt.year.ge(2024)
            ].reset_index(drop=True)
            for symbol, frame in delayed.items()
        }
        delay_validation[str(delay)] = executable_metrics(
            delayed_oos,
            preferred_symbol=preferred,
            fallback_symbol=fallback,
            starting_equity=15.153,
            risk_pct=0.10,
            max_leverage=2.0,
        )
    output = {
        "experiment": "s0_market_tsmom_exchange_executable_audit",
        "rule": (
            "Freeze V2 market gate and exits; apply contract step size, margin, "
            "hard-stop headroom, and stress costs. ETH fallback is operational "
            "only when BTC cannot satisfy the minimum contract."
        ),
        "starting_equity": 15.153,
        "scenarios": scenarios,
        "selected_operational_rule": "BTC preferred; ETH only when BTC minimum contract cannot be sized",
        "selected_validation": validation,
        "selected_oos_execution_delays": delay_validation,
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "report.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
