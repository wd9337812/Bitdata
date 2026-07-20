from app.v4_evidence import filter_live_eligible_v4_shadows, is_live_eligible_v4_shadow


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

