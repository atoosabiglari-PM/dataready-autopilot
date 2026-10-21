"""Tests for proposal contracts; no model calls or dataset changes."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.core.candidates import MAX_CANDIDATES, CandidateBatch, RepairCandidate
from app.core.policy import RepairPlan

FINGERPRINT = "a" * 64


def candidate_payload(candidate_id="candidate-a", action="REMOVE_EXACT_DUPLICATES"):
    return {
        "candidate_id": candidate_id,
        "repair_plan": {
            "source_fingerprint_sha256": FINGERPRINT,
            "summary": "A proposed repair, not an authorization.",
            "actions": [{
                "action": action,
                "justification": "Test fixture for contract validation.",
                "columns": [] if action == "REMOVE_EXACT_DUPLICATES" else ["item"],
            }],
        },
    }


def batch_payload(*candidates):
    return {"source_fingerprint_sha256": FINGERPRINT, "candidates": list(candidates)}


def test_accepts_two_distinct_candidates():
    batch = CandidateBatch.model_validate(batch_payload(
        candidate_payload(),
        candidate_payload("candidate-b", "TRIM_OUTER_WHITESPACE"),
    ))
    assert len(batch.candidates) == 2
    assert isinstance(batch.candidates[0].repair_plan, RepairPlan)


def test_empty_batch_allows_abstention():
    assert CandidateBatch(source_fingerprint_sha256=FINGERPRINT).candidates == []


def test_one_candidate_does_not_require_inventing_an_alternative():
    batch = CandidateBatch.model_validate(batch_payload(candidate_payload()))
    assert len(batch.candidates) == 1


def test_no_action_plan_is_valid_but_not_authorized():
    payload = candidate_payload()
    payload["repair_plan"]["actions"] = []
    candidate = RepairCandidate.model_validate(payload)
    assert candidate.repair_plan.actions == []
    assert not hasattr(candidate, "can_execute")
    assert not hasattr(candidate, "policy_decision")


@pytest.mark.parametrize("bad", ["", "a" * 63, "g" * 64, "A" * 64])
def test_rejects_invalid_source_fingerprint(bad):
    with pytest.raises(ValidationError):
        CandidateBatch(source_fingerprint_sha256=bad)


def test_rejects_candidates_from_different_sources():
    payload = candidate_payload()
    payload["repair_plan"]["source_fingerprint_sha256"] = "b" * 64
    with pytest.raises(ValidationError, match="same source fingerprint"):
        CandidateBatch.model_validate(batch_payload(payload))


def test_rejects_duplicate_ids():
    with pytest.raises(ValidationError, match="IDs must be unique"):
        CandidateBatch.model_validate(batch_payload(
            candidate_payload(),
            candidate_payload(action="TRIM_OUTER_WHITESPACE"),
        ))


def test_rejects_same_actions_with_different_narratives():
    first = candidate_payload()
    second = candidate_payload("candidate-b")
    second["repair_plan"]["summary"] = "Different wording is not a different strategy."
    second["repair_plan"]["actions"][0]["justification"] = "Another explanation."
    with pytest.raises(ValidationError, match="Duplicate repair strategies"):
        CandidateBatch.model_validate(batch_payload(first, second))


def test_column_order_does_not_create_a_new_strategy():
    first = candidate_payload(action="TRIM_OUTER_WHITESPACE")
    first["repair_plan"]["actions"][0]["columns"] = ["item", "category"]
    second = deepcopy(first)
    second["candidate_id"] = "candidate-b"
    second["repair_plan"]["actions"][0]["columns"] = ["category", "item"]
    with pytest.raises(ValidationError, match="Duplicate repair strategies"):
        CandidateBatch.model_validate(batch_payload(first, second))


def test_action_order_is_preserved():
    first = candidate_payload()
    trim = candidate_payload(
        action="TRIM_OUTER_WHITESPACE"
    )["repair_plan"]["actions"][0]
    first["repair_plan"]["actions"].append(trim)
    second = deepcopy(first)
    second["candidate_id"] = "candidate-b"
    second["repair_plan"]["actions"].reverse()
    batch = CandidateBatch.model_validate(batch_payload(first, second))
    assert len(batch.candidates) == 2


def test_rejects_too_many_candidates():
    proposals = [
        candidate_payload(f"candidate-{i}")
        for i in range(MAX_CANDIDATES + 1)
    ]
    with pytest.raises(ValidationError):
        CandidateBatch.model_validate(batch_payload(*proposals))


@pytest.mark.parametrize("field", ["can_execute", "policy_decision", "status", "winner"])
def test_candidate_cannot_supply_authority_or_results(field):
    payload = candidate_payload()
    payload[field] = True
    with pytest.raises(ValidationError):
        RepairCandidate.model_validate(payload)


@pytest.mark.parametrize("level", ["batch", "plan", "action"])
def test_rejects_unexpected_nested_fields(level):
    payload = batch_payload(candidate_payload())
    target = payload
    if level == "plan":
        target = payload["candidates"][0]["repair_plan"]
    elif level == "action":
        target = payload["candidates"][0]["repair_plan"]["actions"][0]
    target["can_execute"] = True
    with pytest.raises(ValidationError):
        CandidateBatch.model_validate(payload)


@pytest.mark.parametrize("bad", ["", "../escape", "candidate a", "x" * 65])
def test_rejects_invalid_candidate_ids(bad):
    with pytest.raises(ValidationError):
        RepairCandidate.model_validate(candidate_payload(bad))


def test_rejects_unknown_action():
    with pytest.raises(ValidationError):
        RepairCandidate.model_validate(
            candidate_payload(action="RUN_ARBITRARY_PYTHON")
        )


def test_known_action_is_a_proposal_not_permission():
    candidate = RepairCandidate.model_validate(
        candidate_payload(action="DROP_ROWS")
    )
    assert candidate.repair_plan.actions[0].action.value == "DROP_ROWS"
    assert not hasattr(candidate, "can_execute")


def test_json_round_trip():
    batch = CandidateBatch.model_validate(batch_payload(candidate_payload()))
    assert CandidateBatch.model_validate_json(batch.model_dump_json()) == batch


def test_candidate_copies_an_existing_repair_plan():
    original = RepairPlan.model_validate(candidate_payload()["repair_plan"])
    candidate = RepairCandidate(candidate_id="candidate-a", repair_plan=original)
    original.actions.clear()
    assert len(candidate.repair_plan.actions) == 1


def test_batch_revalidates_mutated_candidate_inputs():
    candidate = RepairCandidate.model_validate(candidate_payload())
    candidate.repair_plan.source_fingerprint_sha256 = "invalid"
    with pytest.raises(ValidationError):
        CandidateBatch.model_validate(batch_payload(candidate))
