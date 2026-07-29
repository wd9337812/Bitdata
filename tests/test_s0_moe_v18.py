from __future__ import annotations

import pandas as pd

from scripts.train_s0_moe_v1_8 import prepare_public, rolling_group_splits


def _frame(groups: int = 100) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "event_group_id": [f"group-{index}" for index in range(groups)],
            "opportunity_id": [f"opportunity-{index}" for index in range(groups)],
            "time": [
                pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(minutes=index * 5)
                for index in range(groups)
            ],
            "symbol": ["BTCUSDT"] * groups,
            "direction": ["LONG"] * groups,
            "net_pct": [0.1] * groups,
            "base_weight": [1.0] * groups,
        }
    )


def test_prepare_public_applies_explicit_round_trip_cost_and_caps_wave_weight():
    public = _frame(8)
    public["event_group_id"] = "ignored"
    prepared = prepare_public(public)

    assert prepared.cost_pct.eq(0.12).all()
    assert prepared.net_stress_1_0.round(8).eq(public.net_pct.round(8)).all()
    assert prepared.net_stress_2_0.lt(prepared.net_stress_1_0).all()
    assert prepared.groupby("event_group_id").base_weight.sum().max() <= 0.080001


def test_rolling_splits_are_time_ordered_and_event_isolated():
    exact = _frame(100)
    public = exact.iloc[:0].copy()
    splits = rolling_group_splits(public, exact, folds=3)

    assert len(splits) == 3
    for train, validation, test, report in splits:
        train_groups = set(train.event_group_id)
        validation_groups = set(validation.event_group_id)
        test_groups = set(test.event_group_id)
        assert not train_groups & validation_groups
        assert not train_groups & test_groups
        assert not validation_groups & test_groups
        assert not any(report["overlap"].values())
        assert train.time.max() < validation.time.min()
        assert validation.time.max() < test.time.min()
