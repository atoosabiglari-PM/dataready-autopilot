"""Connect candidate trials to audit-regression checks, without selection or release.

These results are evidence, not execution tokens. NO_AUDIT_REGRESSION is not
proof of cell-level correctness, business validity, or production readiness.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from app.core.candidates import CandidateBatch, RepairCandidate
from app.core.policy import DatasetPolicy, PolicyDecision, validate_repair_plan
from app.services.candidate_trials import CandidateTrialResult, run_candidate_trials
from app.services.comparison import build_readiness_comparison
from app.services.regression import (
    RegressionAssessment,
    RegressionInputError,
    assess_audit_regressions,
)
from app.tools.audit import AuditReport

EvaluationStatus = Literal[
    "DENIED", "REQUIRES_REVIEW", "UNSUPPORTED", "TRIAL_FAILED", "OUTPUT_BLOCKED",
    "EVIDENCE_INVALID", "REGRESSION_DETECTED", "NO_AUDIT_REGRESSION",
]


class CandidateEvaluationError(RuntimeError):
    """The trial results do not match the requested candidate batch."""


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    """One trial and its audit-regression outcome; no release permission."""

    trial: CandidateTrialResult
    status: EvaluationStatus
    regression: RegressionAssessment | None = None
    message: str = ""


def _evaluate_trial(
    trial: CandidateTrialResult, policy: DatasetPolicy,
) -> CandidateEvaluation:
    """Check completed-trial evidence; preserve non-completed trial outcomes."""
    if trial.status in {
        "DENIED", "REQUIRES_REVIEW", "UNSUPPORTED", "TRIAL_FAILED", "OUTPUT_BLOCKED",
    }:
        return CandidateEvaluation(
            trial, trial.status, message="No audit-regression pass is claimed for this trial."
        )

    try:
        if trial.status != "REAUDITED":
            raise RegressionInputError("Unrecognized trial status.")
        if (
            trial.audit_after is None
            or trial.comparison is None
            or trial.output_fingerprint_sha256 is None
        ):
            raise RegressionInputError("Completed trial evidence is missing.")

        candidate = RepairCandidate.model_validate(
            trial.assessment.candidate.model_dump(mode="python")
        )
        decision = PolicyDecision.model_validate(
            trial.assessment.policy_decision.model_dump(mode="python")
        )
        before = AuditReport.model_validate(trial.audit_before.model_dump(mode="python"))
        after = AuditReport.model_validate(trial.audit_after.model_dump(mode="python"))
        expected_decision = validate_repair_plan(before, candidate.repair_plan, policy)

        if (
            decision != expected_decision
            or decision.status != "APPROVED"
            or not decision.can_execute
        ):
            raise RegressionInputError("Completed trial lacks matching policy evidence.")
        if trial.comparison != build_readiness_comparison(before, after):
            raise RegressionInputError("Comparison does not match the trial audits.")

        regression = assess_audit_regressions(
            before,
            after,
            candidate.repair_plan,
            expected_output_fingerprint_sha256=trial.output_fingerprint_sha256,
        )
    except (RegressionInputError, ValidationError):
        return CandidateEvaluation(
            trial, "EVIDENCE_INVALID",
            message="Evidence is incomplete or inconsistent; no regression pass is claimed.",
        )

    return CandidateEvaluation(
        trial, regression.status, regression,
        "Audit checks completed. No candidate was selected or released.",
    )


def evaluate_candidate_batch(
    source_file: str | Path,
    batch: CandidateBatch,
    *,
    dataset_policy: DatasetPolicy | None = None,
) -> tuple[CandidateEvaluation, ...]:
    """Run fresh trials and assess their evidence. Integrity errors propagate."""
    checked_batch = CandidateBatch.model_validate(batch.model_dump(mode="python"))
    policy = (
        DatasetPolicy() if dataset_policy is None
        else DatasetPolicy.model_validate(dataset_policy.model_dump(mode="python"))
    )
    trials = run_candidate_trials(
        source_file, checked_batch, dataset_policy=policy
    )

    if len(trials) != len(checked_batch.candidates):
        raise CandidateEvaluationError("Trial count does not match the candidate batch.")
    for candidate, trial in zip(checked_batch.candidates, trials, strict=True):
        if trial.assessment.candidate != candidate:
            raise CandidateEvaluationError("Trial candidate does not match the requested plan.")

    return tuple(_evaluate_trial(trial, policy) for trial in trials)
