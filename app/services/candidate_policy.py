"""Independent policy assessments for competing DataReady repair proposals.

This module does not call a model, execute a repair, or authorize release.
An APPROVED policy result is not proof that a repair is supported or correct.
The executor and evaluator must still perform their own checks.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.candidates import CandidateBatch, RepairCandidate
from app.core.policy import DatasetPolicy, PolicyDecision, validate_repair_plan
from app.tools.audit import AuditReport


@dataclass(frozen=True, slots=True)
class CandidatePolicyAssessment:
    """A proposal paired with its independently computed policy result."""

    candidate: RepairCandidate
    policy_decision: PolicyDecision


def assess_candidate_policies(
    audit_report: AuditReport,
    batch: CandidateBatch,
    *,
    dataset_policy: DatasetPolicy | None = None,
) -> tuple[CandidatePolicyAssessment, ...]:
    """Apply existing policy separately to every candidate, without execution.

    Rebuild validated input snapshots so mutated model instances are checked
    and returned objects do not share mutable state with the caller's inputs.
    These are assessments, not reusable execution tokens. The execution layer
    must check the actual source file and permissions again before any repair.
    """
    checked_batch = CandidateBatch.model_validate(batch.model_dump(mode="python"))
    checked_report = AuditReport.model_validate(audit_report.model_dump(mode="python"))
    checked_policy = (
        DatasetPolicy()
        if dataset_policy is None
        else DatasetPolicy.model_validate(dataset_policy.model_dump(mode="python"))
    )

    return tuple(
        CandidatePolicyAssessment(
            candidate=candidate,
            policy_decision=validate_repair_plan(
                audit_report=checked_report,
                repair_plan=candidate.repair_plan,
                dataset_policy=checked_policy,
            ),
        )
        for candidate in checked_batch.candidates
    )
