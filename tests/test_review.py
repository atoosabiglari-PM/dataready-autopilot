"""Tests for governed human-review resolution."""

import pytest

from app.core.policy import (
    ActionDecision,
    PolicyDecision,
    RepairAction,
)
from app.services.review import (
    HumanReviewError,
    resolve_human_review,
)

FINGERPRINT = "a" * 64


def make_review_decision() -> PolicyDecision:
    """Create a policy decision containing two reviewable actions."""

    return PolicyDecision(
        status="REQUIRES_REVIEW",
        can_execute=False,
        policy_id="review-policy",
        source_fingerprint_sha256=FINGERPRINT,
        action_decisions=[
            ActionDecision(
                action=RepairAction.TRIM_OUTER_WHITESPACE,
                decision="REQUIRES_REVIEW",
                reason="Not automatically authorized.",
                columns=["name"],
            ),
            ActionDecision(
                action=RepairAction.REMOVE_EXACT_DUPLICATES,
                decision="REQUIRES_REVIEW",
                reason="Not automatically authorized.",
                columns=[],
            ),
        ],
        reasons=["Human review is required."],
    )


def test_human_can_approve_all_reviewable_actions() -> None:
    decision = make_review_decision()

    result = resolve_human_review(
        decision,
        approved_action_indexes={0, 1},
        reviewer_reference="reviewer-001",
        review_reason="Validated against the source audit.",
    )

    final = result.final_policy_decision

    assert final.status == "APPROVED"
    assert final.can_execute is True
    assert all(action.decision == "APPROVED" for action in final.action_decisions)

    assert result.reviewer_reference == "reviewer-001"
    assert result.approved_action_indexes == (0, 1)


def test_unapproved_review_action_causes_final_denial() -> None:
    decision = make_review_decision()

    result = resolve_human_review(
        decision,
        approved_action_indexes={0},
        reviewer_reference="reviewer-001",
        review_reason="Only whitespace trimming was accepted.",
    )

    final = result.final_policy_decision

    assert final.status == "DENIED"
    assert final.can_execute is False
    assert final.action_decisions[0].decision == "APPROVED"
    assert final.action_decisions[1].decision == "DENIED"


def test_human_review_cannot_override_deterministic_denial() -> None:
    decision = PolicyDecision(
        status="DENIED",
        can_execute=False,
        policy_id="security-policy",
        source_fingerprint_sha256=FINGERPRINT,
        action_decisions=[
            ActionDecision(
                action=RepairAction.REDACT_PII,
                decision="DENIED",
                reason="Blocked by deterministic security policy.",
                columns=["email"],
            )
        ],
        reasons=["Security policy denied this plan."],
    )

    with pytest.raises(
        HumanReviewError,
        match="cannot be overridden",
    ):
        resolve_human_review(
            decision,
            approved_action_indexes={0},
            reviewer_reference="reviewer-001",
            review_reason="Attempted override.",
        )


def test_rejects_invalid_action_index() -> None:
    decision = make_review_decision()

    with pytest.raises(
        HumanReviewError,
        match="not awaiting review",
    ):
        resolve_human_review(
            decision,
            approved_action_indexes={0, 5},
            reviewer_reference="reviewer-001",
            review_reason="Invalid index test.",
        )


def test_requires_reviewer_identity_and_reason() -> None:
    decision = make_review_decision()

    with pytest.raises(
        HumanReviewError,
        match="reviewer reference is required",
    ):
        resolve_human_review(
            decision,
            approved_action_indexes={0, 1},
            reviewer_reference="   ",
            review_reason="Approved.",
        )

    with pytest.raises(
        HumanReviewError,
        match="human-review reason is required",
    ):
        resolve_human_review(
            decision,
            approved_action_indexes={0, 1},
            reviewer_reference="reviewer-001",
            review_reason="   ",
        )
