from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_s0_market_tsmom_executable import executable_metrics
from scripts.benchmark_s0_market_tsmom_28d import (
    DEFAULT_DATA,
    STRESS_ONE_WAY_COST,
    build_daily_panel,
    market_state,
    symbol_proxy_trades,
)
from scripts.benchmark_s0_market_tsmom_consensus import consensus_state
from scripts.benchmark_s0_market_tsmom_protection import (
    load_hourly_btc,
    simulate_protection,
)


OUTPUT = ROOT / "data" / "research" / "s0_btc_consensus_small_account"


def protected_trades(panel: pd.DataFrame) -> pd.DataFrame:
    signals = symbol_proxy_trades(
        panel,
        consensus_state(market_state(panel), 56),
        "BTCUSDT",
    )
    trades = simulate_protection(
        signals,
        load_hourly_btc(DEFAULT_DATA),
        take_profit_pct=None,
        stop_loss_pct=0.10,
        one_way_cost=STRESS_ONE_WAY_COST,
    )
    trades["initial_stop_pct"] = 0.10
    return trades


def sliced_metrics(
    trades: pd.DataFrame,
    *,
    start_year: int,
    end_year: int,
    capital: float,
    risk: float,
    leverage: float,
) -> dict[str, Any]:
    years = pd.to_datetime(trades.entry_day, utc=True).dt.year
    sliced = trades.loc[years.between(start_year, end_year)].reset_index(drop=True)
    return executable_metrics(
        {"BTCUSDT": sliced},
        preferred_symbol="BTCUSDT",
        fallback_symbol=None,
        starting_equity=capital,
        risk_pct=risk,
        max_leverage=leverage,
    )


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    trades = protected_trades(build_daily_panel(DEFAULT_DATA))
    scenarios: dict[str, Any] = {}
    for capital in (15.153, 25.153, 30.0, 50.0):
        for risk in (0.15, 0.20, 0.30):
            for leverage in (2.0, 3.0):
                key = f"capital_{capital:g}_risk_{int(risk * 100)}_lev_{int(leverage)}"
                scenarios[key] = {
                    "train_2020_2023": sliced_metrics(
                        trades,
                        start_year=2020,
                        end_year=2023,
                        capital=capital,
                        risk=risk,
                        leverage=leverage,
                    ),
                    "oos_2024_2026": sliced_metrics(
                        trades,
                        start_year=2024,
                        end_year=2026,
                        capital=capital,
                        risk=risk,
                        leverage=leverage,
                    ),
                    "current_2026": sliced_metrics(
                        trades,
                        start_year=2026,
                        end_year=2026,
                        capital=capital,
                        risk=risk,
                        leverage=leverage,
                    ),
                }
    result = {
        "experiment": "s0_btc_28_56_consensus_exchange_executable",
        "rule": "long-only BTC 28/56-day market consensus, five-day hold, 10% exchange stop",
        "cost": "0.12% one way, 0.24% round trip",
        "exchange_filter": "BTCUSDT minQty 0.001, step 0.001, minNotional 50 USDT",
        "scenarios": scenarios,
    }
    (OUTPUT / "report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
