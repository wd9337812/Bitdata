from __future__ import annotations

import pandas as pd
import pytest

from scripts.audit_s0_cross_sectional_momentum import (
    block_bootstrap,
    contribution_audit,
    enrich_trades,
    select_development_hypothesis,
    summarize,
)


def test_enrich_trades_uses_official_onboard_date() -> None:
    trades = pd.DataFrame(
        {
            "profile": ["momentum_24h_5pct"],
            "execution_mode": ["minute_delay_1m"],
            "signal_ms": [10 * 86_400_000],
            "entry_ms": [10 * 86_400_000 + 60_000],
            "symbol": ["AUSDT"],
            "direction": ["LONG"],
            "strength": [0.99],
            "net_pct": [1.0],
        }
    )
    panel = pd.DataFrame(
        {
            "available_ms": [10 * 86_400_000, 10 * 86_400_000],
            "symbol": ["AUSDT", "BTCUSDT"],
            "ret_24h": [0.1, 0.02],
            "liquidity_24h": [100.0, 1_000.0],
        }
    )
    result = enrich_trades(
        trades,
        panel,
        {"AUSDT": 5 * 86_400_000, "BTCUSDT": 0},
    )
    assert result.loc[0, "symbol_age_days"] == 5.0
    assert result.loc[0, "market_breadth_24h"] == pytest.approx(0.06)
    assert result.loc[0, "btc_ret_24h"] == 0.02


def test_block_bootstrap_reports_probability_of_non_positive_net() -> None:
    frame = pd.DataFrame(
        {
            "entry_time": pd.to_datetime(
                ["2026-01-01", "2026-01-08"],
                utc=True,
            ),
            "net_pct": [1.0, 1.0],
        }
    )
    result = block_bootstrap(frame, samples=100, seed=7)
    assert result["calendar_weeks"] == 2
    assert result["probability_net_not_positive"] == 0.0
    assert result["net_pct_points_p025"] == 2.0


def test_contribution_audit_exposes_top_symbol_dependence() -> None:
    frame = pd.DataFrame(
        {
            "symbol": ["A", "A", "B", "C"],
            "net_pct": [2.0, 2.0, -1.0, 1.0],
        }
    )
    result = contribution_audit(frame)
    assert result["top_10"]["A"] == 4.0
    assert result["remove_top_1"]["net_pct_points"] == 0.0


def test_development_selection_does_not_use_later_windows() -> None:
    baseline = {
        "trades": 100,
        "profit_factor": 1.1,
        "net_pct_points": 10.0,
        "max_drawdown_pct_points": 20.0,
    }
    better = {
        "trades": 90,
        "profit_factor": 1.2,
        "net_pct_points": 12.0,
        "max_drawdown_pct_points": 18.0,
    }
    hypotheses = {
        "baseline": {
            "windows": {
                "development": baseline,
                "validation": summarize(pd.DataFrame()),
                "test": summarize(pd.DataFrame()),
                "final_july": summarize(pd.DataFrame()),
            }
        },
        "candidate": {
            "windows": {
                "development": better,
                "validation": {"net_pct_points": -999.0},
                "test": {"net_pct_points": -999.0},
                "final_july": {"net_pct_points": -999.0},
            }
        },
    }
    result = select_development_hypothesis(hypotheses)
    assert result["selected"] == "candidate"
    assert result["selection_scope"] == "development_only"
