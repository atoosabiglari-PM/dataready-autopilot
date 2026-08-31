"""Human-review resolution for governed DataReady repair decisions."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.policy import ActionDecision, PolicyDecision


class HumanReviewError(RuntimeError):
    """Raised when a human-review decision is invalid or unsafe."""


@dataclass(frozen=True)
class HumanReviewResolution:
    """Evidence describing one completed human-review decision."""

    reviewer_reference: str
    review_reason: str
    approved_action_indexes: tuple[int, ...]
    final_policy_decision: PolicyDecision


def resolve_human_review(
    policy_decision: PolicyDecision,
    *,
    approved_action_indexes: set[int],
    reviewer_reference: str,
    review_reason: str,
) -> HumanReviewResolution:
    """Resolve actions requiring review without overriding deterministic denials."""

    reviewer = reviewer_reference.strip()
    reason = review_reason.strip()

    if not reviewer:
        raise HumanReviewError("A reviewer reference is required.")

    if not reason:
        raise HumanReviewError("A human-review reason is required.")

    if policy_decision.status == "DENIED":
        raise HumanReviewError(
            "A deterministically denied policy decision cannot be overridden through human review."
        )

    if any(decision.decision == "DENIED" for decision in policy_decision.action_decisions):
        raise HumanReviewError("A denied repair action cannot be overridden through human review.")

    if policy_decision.status != "REQUIRES_REVIEW":
        raise HumanReviewError("This policy decision does not require human review.")

    if policy_decision.source_fingerprint_sha256 is None:
        raise HumanReviewError("Human review requires a source fingerprint.")

    reviewable_indexes = {
        index
        for index, decision in enumerate(policy_decision.action_decisions)
        if decision.decision == "REQUIRES_REVIEW"
    }

    if not reviewable_indexes:
        raise HumanReviewError("The policy decision contains no reviewable repair actions.")

    invalid_indexes = approved_action_indexes - reviewable_indexes

    if invalid_indexes:
        raise HumanReviewError("Human approval referenced an action that is not awaiting review.")

    final_action_decisions: list[ActionDecision] = []

    for index, decision in enumerate(policy_decision.action_decisions):
        if decision.decision == "APPROVED":
            final_action_decisions.append(decision)
            continue

        if index in approved_action_indexes:
            final_action_decisions.append(
                ActionDecision(
                    action=decision.action,
                    decision="APPROVED",
                    reason=(f"Human review approved by {reviewer!r}: {reason}"),
                    columns=decision.columns,
                )
            )
        else:
            final_action_decisions.append(
                ActionDecision(
                    action=decision.action,
                    decision="DENIED",
                    reason=(
                        f"Human review did not approve this action. Reviewer {reviewer!r}: {reason}"
                    ),
                    columns=decision.columns,
                )
            )

    all_approved = all(decision.decision == "APPROVED" for decision in final_action_decisions)

    if all_approved:
        final_policy_decision = PolicyDecision(
            status="APPROVED",
            can_execute=True,
            policy_id=policy_decision.policy_id,
            source_fingerprint_sha256=(policy_decision.source_fingerprint_sha256),
            action_decisions=final_action_decisions,
            reasons=[
                *policy_decision.reasons,
                f"Human review completed by {reviewer!r}: {reason}",
            ],
            invariants_enforced=policy_decision.invariants_enforced,
        )
    else:
        final_policy_decision = PolicyDecision(
            status="DENIED",
            can_execute=False,
            policy_id=policy_decision.policy_id,
            source_fingerprint_sha256=(policy_decision.source_fingerprint_sha256),
            action_decisions=final_action_decisions,
            reasons=[
                *policy_decision.reasons,
                f"Human review rejected one or more actions. Reviewer {reviewer!r}: {reason}",
            ],
            invariants_enforced=policy_decision.invariants_enforced,
        )

    return HumanReviewResolution(
        reviewer_reference=reviewer,
        review_reason=reason,
        approved_action_indexes=tuple(sorted(approved_action_indexes)),
        final_policy_decision=final_policy_decision,
    )
