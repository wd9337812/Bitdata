from __future__ import annotations

from app.s0_event_override import (
    EventOverrideCandidate,
    EventOverrideConfig,
    EventOverrideState,
)


def _strong_candidate() -> EventOverrideCandidate:
    return EventOverrideCandidate(
        name="example_event_v1",
        symbol="SOLUSDT",
        direction=1,
        expected_net_pct=1.5,
        evidence_pf=1.35,
        evidence_trades=40,
    )


def test_disabled_channel_never_overrides() -> None:
    state = EventOverrideState(
        config=EventOverrideConfig(enabled=False),
        candidates=[_strong_candidate()],
    )
    decision = state.decide()
    assert decision["override"] is None
    assert decision["reason"] == "channel_disabled"


def test_eligible_candidate_produces_dry_run_override() -> None:
    state = EventOverrideState(
        config=EventOverrideConfig(enabled=True),
        candidates=[_strong_candidate()],
    )
    decision = state.decide()
    assert decision["override"] is not None
    assert decision["override"]["symbol"] == "SOLUSDT"
    assert decision["dry_run"] is True


def test_weak_candidate_is_rejected() -> None:
    weak = EventOverrideCandidate(
        name="weak_v1",
        symbol="DOGEUSDT",
        direction=-1,
        expected_net_pct=0.1,
        evidence_pf=1.05,
        evidence_trades=12,
    )
    state = EventOverrideState(
        config=EventOverrideConfig(enabled=True),
        candidates=[weak],
    )
    assert state.decide()["reason"] == "no_qualified_candidate"


def test_status_payload_matches_frontend_card() -> None:
    state = EventOverrideState(
        config=EventOverrideConfig(enabled=True),
        candidates=[_strong_candidate()],
    )
    payload = state.status_payload()
    assert payload["event_channel_status"] == "有合格候选"
    assert payload["qualified_candidates"][0]["symbol"] == "SOLUSDT"
    assert payload["takeover_rules"]["priority"] == "事件 > 底仓"
