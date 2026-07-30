from __future__ import annotations

import pandas as pd

from scripts.benchmark_s0_exit_profiles import attach_research_gates, relabel_profile


def test_relabel_profile_uses_stop_first_for_same_minute_ambiguity():
    minute = pd.DataFrame(
        {
            "open_time": [300_000, 360_000],
            "open": [100.0, 100.0],
            "high": [102.0, 100.0],
            "low": [98.0, 100.0],
            "close": [101.0, 100.0],
        }
    )
    candidates = pd.DataFrame(
        {
            "open_time": [0],
            "entry": [100.0],
            "stop": [99.15],
            "direction": ["LONG"],
        }
    )
    result = relabel_profile(
        minute,
        candidates,
        stop_atr=0.85,
        take_profit_r=1.05,
        hold_minutes=1,
    )

    assert result.iloc[0].outcome == "STOP"
    assert result.iloc[0].net_pct < -0.9


def test_relabel_profile_changes_time_exit_with_holding_window():
    minute = pd.DataFrame(
        {
            "open_time": [300_000, 360_000, 420_000],
            "open": [100.0, 100.2, 100.5],
            "high": [100.3, 100.6, 100.8],
            "low": [99.9, 100.1, 100.4],
            "close": [100.2, 100.5, 100.7],
        }
    )
    candidates = pd.DataFrame(
        {
            "open_time": [0],
            "entry": [100.0],
            "stop": [99.15],
            "direction": ["LONG"],
        }
    )
    one = relabel_profile(
        minute,
        candidates,
        stop_atr=2.0,
        take_profit_r=3.0,
        hold_minutes=1,
    )
    three = relabel_profile(
        minute,
        candidates,
        stop_atr=2.0,
        take_profit_r=3.0,
        hold_minutes=3,
    )

    assert three.iloc[0].net_pct > one.iloc[0].net_pct


def test_v49_proxy_requires_historical_thresholds_and_structure():
    frame = pd.DataFrame(
        {
            "direction": ["LONG", "LONG", "LONG"],
            "setup_type": ["momentum", "momentum", "breakout"],
            "market_regime": ["broad_up", "broad_up", "broad_up"],
            "rank_percentile": [0.90, 0.79, 0.90],
            "quality": [0.70, 0.70, 0.70],
            "expected_net_pct": [0.10, 0.10, 0.10],
            "lower_expected_net_pct": [0.02, 0.02, 0.02],
            "cost_ratio": [2.0, 2.0, 2.0],
            "confirmations": [4, 4, 4],
            "medium_alignment": [1.0, 1.0, 1.0],
            "volume_persistence": [0.70, 0.70, 0.70],
            "directed_flow": [0.80, 0.80, 0.80],
            "medium_path": [0.50, 0.50, 0.50],
            "anti_chase": [0.70, 0.70, 0.70],
            "regime_fit": [0.90, 0.90, 0.90],
            "entry_quality": [0.80, 0.80, 0.80],
            "extension_atr": [0.20, 0.20, 0.20],
            "impulse_atr": [0.50, 0.50, 0.50],
            "volume_acceleration": [1.20, 1.20, 1.20],
            "micro_adverse_wick": [0.20, 0.20, 0.20],
        }
    )

    result = attach_research_gates(frame)

    assert result.v49_static_proxy.tolist() == [True, False, False]


def test_v49_proxy_blocks_late_exhaustion():
    frame = pd.DataFrame(
        {
            "direction": ["SHORT"],
            "setup_type": ["prebreakout"],
            "market_regime": ["broad_down"],
            "rank_percentile": [0.95],
            "quality": [0.80],
            "expected_net_pct": [0.20],
            "lower_expected_net_pct": [0.10],
            "cost_ratio": [4.0],
            "confirmations": [5],
            "medium_alignment": [1.0],
            "volume_persistence": [0.80],
            "directed_flow": [0.90],
            "medium_path": [0.05],
            "anti_chase": [0.80],
            "regime_fit": [0.95],
            "entry_quality": [0.90],
            "extension_atr": [1.20],
            "impulse_atr": [2.00],
            "volume_acceleration": [0.60],
            "micro_adverse_wick": [3.00],
        }
    )

    result = attach_research_gates(frame)

    assert result.iloc[0].v49_exhaustion_score >= 0.62
    assert not result.iloc[0].v49_static_proxy
