from __future__ import annotations

import pandas as pd

from scripts.research_s0_15m_tail_lgbm import add_labels_and_features


def test_mfe_labels_are_forward_looking() -> None:
    rows = []
    for index in range(20):
        rows.append(
            {
                "open_time": index * 900_000,
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "quote_volume": 1_000_000.0,
                "taker_buy_quote": 500_000.0,
            }
        )
    frame = pd.DataFrame(rows)
    frame.loc[5, "high"] = 110.0
    frame.loc[7, "low"] = 90.0
    out = add_labels_and_features(frame)
    assert bool(out.loc[0, "label_long"]) is True
    assert bool(out.loc[0, "label_short"]) is True
    # Last rows have no full 8-bar future and are dropped.
    assert len(out) == 12
