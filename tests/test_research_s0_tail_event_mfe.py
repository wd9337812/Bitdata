import pandas as pd

from scripts.research_s0_tail_event_mfe import (
    LABEL_THRESHOLD_PCT,
    extract_candidates,
)


def _panel() -> pd.DataFrame:
    rows = []
    # Align to a UTC day boundary so candidates are extracted.
    base = 1_599_955_200_000
    for hour in range(300):
        # Big 50% spike starting at hour 100 so the day-0 candidate labels 1.
        spike = 1.5 if 100 <= hour < 160 else 1.0
        rows.append(
            {
                "symbol": "TESTUSDT",
                "available_ms": base + hour * 3_600_000,
                "open": 100.0 * spike,
                "high": 105.0 * spike,
                "low": 99.0 * spike,
                "close": 102.0 * spike,
                "quote_volume": 1_000_000.0,
                "symbol_age_days": 300.0,
                "liquidity_24h": 100_000_000.0,
                "atr_24h": 1.0,
                "ret_24h": 0.01,
                "ret_168h": 0.05,
                "ret_720h": 0.10,
                "funding_rate_pct": 0.01,
            }
        )
    return pd.DataFrame(rows)


def test_extract_candidates_labels_big_mfe():
    panel = _panel()

    candidates = extract_candidates(panel)

    assert len(candidates) >= 2
    first = candidates.iloc[0]
    assert first["mfe_max_pct"] > LABEL_THRESHOLD_PCT
    assert first["label"] == 1
    assert {"volume_shock", "ret_720h", "funding_rate_pct"}.issubset(
        candidates.columns
    )
