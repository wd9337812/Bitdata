from app.v4_evidence import (
    executable_single_position_shadows,
    filter_live_eligible_v4_shadows,
    is_live_eligible_v4_shadow,
)


def test_v44_uses_only_full_bet_decision_shadows_for_live_recovery():
    rows = [
        {"id": 1, "evidence_type": "decision", "payload": '{"admission_lane":"full_bet"}'},
        {"id": 2, "evidence_type": "decision", "payload": '{"admission_lane":"shadow_only"}'},
        {"id": 3, "evidence_type": "exploration", "payload": '{"admission_lane":"full_bet"}'},
        {"id": 4, "evidence_type": "decision", "payload": '{"admission_lane":"core_canary"}'},
    ]

    eligible = filter_live_eligible_v4_shadows(rows, strategy_version="v4.4")

    assert [row["id"] for row in eligible] == [1]
    assert eligible[0]["admission_lane"] == "full_bet"


def test_v43_keeps_legacy_live_lanes_but_rejects_v44_full_bet_lane():
    assert is_live_eligible_v4_shadow(
        {"evidence_type": "decision", "admission_lane": "core_canary"},
        strategy_version="v4.3.2",
    )
    assert not is_live_eligible_v4_shadow(
        {"evidence_type": "decision", "admission_lane": "full_bet"},
        strategy_version="v4.3.2",
    )


def test_live_recovery_deduplicates_same_opportunity_id():
    rows = [
        {
            "id": 1,
            "opportunity_id": "same-opportunity",
            "evidence_type": "decision",
            "admission_lane": "full_bet",
        },
        {
            "id": 2,
            "opportunity_id": "same-opportunity",
            "evidence_type": "decision",
            "admission_lane": "full_bet",
        },
    ]

    eligible = filter_live_eligible_v4_shadows(rows, strategy_version="v4.4")

    assert len(eligible) == 1


def test_v45_recovery_uses_only_non_overlapping_single_position_path():
    rows = [
        {"id": 1, "opened_at": "2026-07-20T00:00:00+00:00", "closed_at": "2026-07-20T00:10:00+00:00"},
        {"id": 2, "opened_at": "2026-07-20T00:05:00+00:00", "closed_at": "2026-07-20T00:08:00+00:00"},
        {"id": 3, "opened_at": "2026-07-20T00:10:00+00:00", "closed_at": "2026-07-20T00:20:00+00:00"},
    ]

    executable = executable_single_position_shadows(rows)

    assert [row["id"] for row in executable] == [3, 1]
