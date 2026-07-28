from __future__ import annotations

import pandas as pd

import pytest

pytest.importorskip("joblib")

from scripts.train_s0_moe_v1_7 import (
    cap_event_group_weights,
    group_time_split,
)


def _exact_rows() -> pd.DataFrame:
    rows = []
    for index in range(50):
        group = f"group-{index}"
        for repeat in range(2):
            rows.append(
                {
                    "event_group_id": group,
                    "opportunity_id": f"{group}-{repeat}",
                    "time": pd.Timestamp("2026-01-01", tz="UTC")
                    + pd.Timedelta(minutes=index * 10 + repeat),
                    "source": "v5_shadow_decision",
                    "base_weight": 1.0,
                }
            )
    return pd.DataFrame(rows)


def test_group_time_split_never_leaks_a_market_wave():
    exact = _exact_rows()
    public = exact.iloc[:0].copy()
    train, validation, test, report = group_time_split(public, exact)

    train_groups = set(train.event_group_id)
    validation_groups = set(validation.event_group_id)
    test_groups = set(test.event_group_id)
    assert not train_groups & validation_groups
    assert not train_groups & test_groups
    assert not validation_groups & test_groups
    assert report["group_overlap"] == {
        "train_validation": 0,
        "train_test": 0,
        "validation_test": 0,
    }


def test_repeated_shadow_event_has_capped_aggregate_weight():
    exact = _exact_rows().iloc[:8].copy()
    weighted = cap_event_group_weights(exact)

    assert weighted.groupby("event_group_id").base_weight.sum().max() <= 1.5
