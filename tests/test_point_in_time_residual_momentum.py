import pandas as pd

from scripts.benchmark_s0_point_in_time_residual_momentum import (
    ResidualCandidate,
    add_residual_features,
    residual_signals,
)


def test_residual_features_remove_btc_move() -> None:
    rows = []
    for timestamp in range(800):
        for symbol, extra in (("BTCUSDT", 0.0), ("ALTUSDT", 0.01)):
            rows.append(
                {
                    "symbol": symbol,
                    "available_ms": timestamp,
                    "ret_6h": 0.02 + extra,
                    "ret_24h": 0.04 + extra,
                    "ret_72h": 0.06 + extra,
                    "liquidity_24h": 30_000_000,
                }
            )
    enriched = add_residual_features(pd.DataFrame(rows))
    alt = enriched.loc[enriched.symbol.eq("ALTUSDT")].iloc[-1]
    assert round(float(alt.residual_24h), 8) == 0.01


def test_long_market_selects_highest_residual() -> None:
    panel = pd.DataFrame(
        {
            "symbol": ["AUSDT", "BUSDT"],
            "available_ms": [1, 1],
            "symbol_age_days": [60, 60],
            "liquidity_24h": [30_000_000, 30_000_000],
            "universe_size": [100, 100],
            "market_direction": ["LONG", "LONG"],
            "residual_6h": [0.01, 0.02],
            "residual_24h": [0.01, 0.03],
            "residual_72h": [0.01, 0.04],
            "cross_sectional_dispersion_24h": [0.02, 0.02],
            "dispersion_p50_lagged": [0.03, 0.03],
            "dispersion_p70_lagged": [0.04, 0.04],
        }
    )
    candidate = ResidualCandidate("test", "residual_24h")
    signal = residual_signals(panel, candidate, 30).iloc[0]
    assert signal.symbol == "BUSDT"
    assert signal.direction == "LONG"


def test_dispersion_gate_uses_lagged_threshold() -> None:
    panel = pd.DataFrame(
        {
            "symbol": ["AUSDT"],
            "available_ms": [1],
            "symbol_age_days": [60],
            "liquidity_24h": [30_000_000],
            "universe_size": [100],
            "market_direction": ["LONG"],
            "residual_6h": [0.01],
            "residual_24h": [0.02],
            "residual_72h": [0.03],
            "cross_sectional_dispersion_24h": [0.05],
            "dispersion_p50_lagged": [0.03],
            "dispersion_p70_lagged": [0.04],
        }
    )
    candidate = ResidualCandidate("test", "residual_24h", "p70")
    assert residual_signals(panel, candidate, 30).empty
