"""Run permitted candidates on temporary copies and collect re-audit evidence.

No model calls, arbitrary generated code, ranking, or production release.
Temporary directories isolate files, not operating-system privileges.
REAUDITED means measured, not accepted as a correct repair.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from app.core.candidates import CandidateBatch
from app.core.policy import DatasetPolicy
from app.services.candidate_policy import CandidatePolicyAssessment, assess_candidate_policies
from app.services.comparison import ReadinessComparison, build_readiness_comparison
from app.services.executor import (
    EXECUTABLE_ACTIONS,
    RepairExecutionError,
    execute_repair_plan,
    prepare_execution_context,
)
from app.tools.audit import AuditReport, audit_csv
from app.tools.fingerprint import calculate_sha256
from app.tools.preflight import MAX_FILE_SIZE_BYTES

TrialStatus = Literal[
    "DENIED",
    "REQUIRES_REVIEW",
    "UNSUPPORTED",
    "TRIAL_FAILED",
    "OUTPUT_BLOCKED",
    "REAUDITED",
]


class CandidateTrialError(RuntimeError):
    """An integrity failure invalidates the batch; no results are accepted."""


@dataclass(frozen=True, slots=True)
class CandidateTrialResult:
    """Evidence only. Temporary input/output files are deleted after each trial."""

    assessment: CandidatePolicyAssessment
    status: TrialStatus
    audit_before: AuditReport
    audit_after: AuditReport | None = None
    comparison: ReadinessComparison | None = None
    output_fingerprint_sha256: str | None = None
    message: str = ""


def _check_integrity(path: Path, expected: str) -> None:
    if not path.is_file() or path.is_symlink() or calculate_sha256(path) != expected:
        raise CandidateTrialError("Source integrity changed during candidate trials.")


def run_candidate_trials(
    source_file: str | Path,
    batch: CandidateBatch,
    *,
    dataset_policy: DatasetPolicy | None = None,
) -> tuple[CandidateTrialResult, ...]:
    """Re-audit the source, assess policy, and trial only permitted operations.

    Each trial starts from identical source bytes, never another trial's output.
    Review-required proposals are not executed, even on temporary copies.
    Reports are observations; regression checks and release remain later stages.
    """
    checked_batch = CandidateBatch.model_validate(batch.model_dump(mode="python"))
    source = Path(source_file)
    if source.is_symlink() or not source.is_file():
        raise CandidateTrialError("A regular, non-symlink source file is required.")

    review = dataset_policy.audit_review if dataset_policy is not None else None
    before = audit_csv(source, review_context=review) if review else audit_csv(source)
    assessments = assess_candidate_policies(
        before, checked_batch, dataset_policy=dataset_policy
    )
    fingerprint = before.fingerprint_sha256
    if fingerprint is None:
        raise CandidateTrialError("The source audit has no fingerprint.")
    _check_integrity(source, fingerprint)

    source_bytes: bytes | None = None
    results: list[CandidateTrialResult] = []

    try:
        for assessment in assessments:
            decision = assessment.policy_decision
            plan = assessment.candidate.repair_plan

            if decision.status != "APPROVED" or not decision.can_execute:
                status: TrialStatus = (
                    "DENIED" if decision.status == "DENIED" else "REQUIRES_REVIEW"
                )
                results.append(CandidateTrialResult(
                    assessment,
                    status,
                    before,
                    message="Not executed: policy did not permit it.",
                ))
                continue

            if any(action.action not in EXECUTABLE_ACTIONS for action in plan.actions):
                results.append(CandidateTrialResult(
                    assessment,
                    "UNSUPPORTED",
                    before,
                    message="Not executed: an action is not implemented by the executor.",
                ))
                continue

            if source_bytes is None:
                with source.open("rb") as handle:
                    source_bytes = handle.read(MAX_FILE_SIZE_BYTES + 1)

                if len(source_bytes) > MAX_FILE_SIZE_BYTES:
                    raise CandidateTrialError("Source exceeds the trial size limit.")

                if sha256(source_bytes).hexdigest() != fingerprint:
                    raise CandidateTrialError("Source changed after the audit.")

            with TemporaryDirectory(prefix="dataready-candidate-") as directory:
                trial_source = Path(directory) / "source.csv"
                trial_output = Path(directory) / "output.csv"
                trial_source.write_bytes(source_bytes)

                try:
                    context = prepare_execution_context(
                        trial_source, trial_output, plan, decision
                    )
                    output = execute_repair_plan(context)

                    if output != trial_output or output.is_symlink() or not output.is_file():
                        raise CandidateTrialError("Executor returned an unexpected output.")

                    after = audit_csv(output, review_context=review) if review else audit_csv(output)
                    output_hash = calculate_sha256(output)

                    if after.fingerprint_sha256 != output_hash:
                        raise CandidateTrialError("Output changed during re-audit.")

                    status = (
                        "OUTPUT_BLOCKED" if after.status == "BLOCKED" else "REAUDITED"
                    )
                    comparison = (
                        None if after.status == "BLOCKED"
                        else build_readiness_comparison(before, after)
                    )

                    results.append(CandidateTrialResult(
                        assessment,
                        status,
                        before,
                        after,
                        comparison,
                        output_hash,
                        "Trial measured; not ranked, selected, or released.",
                    ))

                except (RepairExecutionError, OSError, ValueError) as error:
                    results.append(CandidateTrialResult(
                        assessment,
                        "TRIAL_FAILED",
                        before,
                        message=f"{type(error).__name__}: candidate did not complete.",
                    ))

                finally:
                    _check_integrity(trial_source, fingerprint)

    finally:
        _check_integrity(source, fingerprint)

    return tuple(results)
