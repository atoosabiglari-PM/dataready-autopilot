"""Conservative regression checks over audit evidence, not release authorization.

NO_AUDIT_REGRESSION means only that these checks detected no regression.
It does not prove cell-level correctness, business validity, or repair success.
No files are read/written and no model or executor is called here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.core.policy import RepairAction, RepairPlan
from app.tools.audit import AuditReport

SEVERITY_ORDER = {"INFO": 0, "WARNING": 1, "CRITICAL": 2}


class RegressionInputError(ValueError):
    """Audit evidence is missing, invalid, or bound to a different dataset."""


@dataclass(frozen=True, slots=True)
class RegressionFinding:
    """A deterministic reason that a candidate needs rejection or investigation."""

    code: str
    message: str
    issue_code: str | None = None
    column: str | None = None


@dataclass(frozen=True, slots=True)
class RegressionAssessment:
    """Audit-level observations only; never an execution or release permission."""

    findings: tuple[RegressionFinding, ...]

    @property
    def status(self) -> Literal["REGRESSION_DETECTED", "NO_AUDIT_REGRESSION"]:
        return "REGRESSION_DETECTED" if self.findings else "NO_AUDIT_REGRESSION"


def _valid_hash(value: str | None) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _group_findings(report: AuditReport) -> dict[tuple[str, str | None], tuple[int, int]]:
    """Group by code AND column; sum counts and retain the highest severity."""
    grouped: dict[tuple[str, str | None], tuple[int, int]] = {}
    for issue in report.issues:
        key = (issue.code, issue.column)
        old_severity, old_count = grouped.get(key, (-1, 0))
        grouped[key] = (
            max(old_severity, SEVERITY_ORDER[issue.severity]),
            old_count + issue.count,
        )
    return grouped


def assess_audit_regressions(
    before: AuditReport,
    after: AuditReport,
    repair_plan: RepairPlan,
    *,
    expected_output_fingerprint_sha256: str,
) -> RegressionAssessment:
    """Inspect a completed trial's audit evidence without changing anything.

    Supply trial.audit_before, trial.audit_after, its candidate repair_plan,
    and trial.output_fingerprint_sha256. Only completed trials belong here.
    The supplied output hash must come from the trial runner, not the model.
    Hash matching checks consistency of evidence, not authenticity or live files.
    """
    before = AuditReport.model_validate(before.model_dump(mode="python"))
    after = AuditReport.model_validate(after.model_dump(mode="python"))
    plan = RepairPlan.model_validate(repair_plan.model_dump(mode="python"))

    if before.review_context != after.review_context:
        raise RegressionInputError("Before and after audits must use the same human review context.")

    if before.status == "BLOCKED" or before.preflight.status == "BLOCKED":
        raise RegressionInputError("A blocked source cannot be evaluated as a repair trial.")
    if not _valid_hash(before.fingerprint_sha256):
        raise RegressionInputError("The source audit needs a valid fingerprint.")
    if plan.source_fingerprint_sha256 != before.fingerprint_sha256:
        raise RegressionInputError("The repair plan does not match the source audit.")
    if not _valid_hash(expected_output_fingerprint_sha256):
        raise RegressionInputError("A valid trial output fingerprint is required.")
    if after.fingerprint_sha256 != expected_output_fingerprint_sha256:
        raise RegressionInputError("The output audit does not match the trial output fingerprint.")

    findings: list[RegressionFinding] = []
    if after.status == "BLOCKED" or after.preflight.status == "BLOCKED":
        return RegressionAssessment((RegressionFinding(
            "OUTPUT_BLOCKED", "The repaired output failed its audit or preflight gate."
        ),))

    for report in (before, after):
        if report.duplicate_row_count > report.row_count:
            raise RegressionInputError("Duplicate count exceeds the reported row count.")

    if before.status == "READY" and after.status != "READY":
        findings.append(RegressionFinding("READINESS_REGRESSION", "READY became non-READY."))
    if after.quality_score < before.quality_score:
        findings.append(RegressionFinding("QUALITY_SCORE_DECREASED", "Audit quality score fell."))
    if after.column_count != before.column_count:
        findings.append(RegressionFinding("COLUMN_COUNT_CHANGED", "Column count changed."))
    if after.row_count > before.row_count:
        findings.append(RegressionFinding("ROWS_ADDED", "Repair unexpectedly added rows."))

    removed = before.row_count - after.row_count
    has_dedupe = any(
        action.action == RepairAction.REMOVE_EXACT_DUPLICATES for action in plan.actions
    )
    if removed > 0 and not has_dedupe:
        findings.append(RegressionFinding(
            "UNEXPECTED_ROW_REMOVAL", "Rows disappeared without a duplicate-removal action."
        ))
    elif removed > before.duplicate_row_count:
        findings.append(RegressionFinding(
            "EXCESS_ROW_REMOVAL", "Row removal exceeds the original audit's duplicate count."
        ))
    if after.duplicate_row_count > before.duplicate_row_count:
        findings.append(RegressionFinding("DUPLICATES_INCREASED", "Duplicate-row count grew."))

    old = _group_findings(before)
    new = _group_findings(after)
    for key in sorted(new, key=lambda item: (item[0], item[1] or "")):
        issue_code, column = key
        severity, count = new[key]
        if key not in old:
            findings.append(RegressionFinding(
                "NEW_FINDING", "A new finding appeared for this code and column.",
                issue_code, column,
            ))
            continue
        old_severity, old_count = old[key]
        if severity > old_severity:
            findings.append(RegressionFinding(
                "SEVERITY_INCREASED", "An existing finding became more severe.",
                issue_code, column,
            ))
        if count > old_count:
            findings.append(RegressionFinding(
                "FINDING_COUNT_INCREASED", f"Affected count grew from {old_count} to {count}.",
                issue_code, column,
            ))

    return RegressionAssessment(tuple(findings))
