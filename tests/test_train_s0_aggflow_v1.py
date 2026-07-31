from __future__ import annotations

import pandas as pd

from scripts.train_s0_aggflow_v1 import enrich_aggflow, merge_symbol_aggflow


def test_merge_aggflow_is_backward_only() -> None:
    candidates = pd.DataFrame(
        {"open_time": [180_000], "symbol": ["BTCUSDT"], "direction": ["LONG"]}
    )
    flow = pd.DataFrame(
        {
            "available_ms": [120_000, 240_000],
            "agg_trade_count_5m": [10, 20],
        }
    )
    result = merge_symbol_aggflow(candidates, flow)
    assert result.loc[0, "available_ms"] == 120_000
    assert result.loc[0, "agg_trade_count_5m"] == 10
    assert result.loc[0, "aggflow_age_minutes"] == 1.0


def test_enrich_aggflow_directionalizes_taker_flow(tmp_path) -> None:
    columns = {
        "available_ms": [120_000],
        "symbol": ["BTCUSDT"],
        "agg_trade_count_5m": [20],
        "agg_quote_log_5m": [10.0],
        "agg_mean_quote_5m": [100.0],
        "agg_taker_imbalance_1m": [0.2],
        "agg_taker_imbalance_5m": [0.3],
        "agg_taker_imbalance_15m": [0.1],
        "agg_signed_count_ratio_1m": [0.25],
        "agg_flow_persistence_5m": [0.5],
        "agg_flow_persistence_15m": [0.2],
        "agg_price_return_1m": [0.01],
    }
    pd.DataFrame(columns).to_parquet(
        tmp_path / "BTCUSDT-aggtrades.parquet",
        index=False,
    )
    candidates = pd.DataFrame(
        {
            "open_time": [180_000],
            "symbol": ["BTCUSDT"],
            "direction": ["SHORT"],
        }
    )
    result = enrich_aggflow(candidates, tmp_path)
    assert len(result) == 1
    assert result.loc[0, "agg_taker_imbalance_5m_dir"] == -0.3
    assert result.loc[0, "agg_price_return_1m_dir"] == -0.01
