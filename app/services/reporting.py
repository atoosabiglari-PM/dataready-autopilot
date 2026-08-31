"""Machine-readable evidence reporting for DataReady Autopilot."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from app.core.policy import PolicyDecision, RepairPlan
from app.services.comparison import ReadinessComparison
from app.services.lineage import DatasetLineageEvidence
from app.tools.audit import AuditReport

ReportStatus = Literal[
    "BLOCKED",
    "READY",
    "REQUIRES_REVIEW",
    "DENIED",
    "REPAIRED",
]


class MachineReadableReport(BaseModel):
    """Portable JSON evidence produced by one governed Autopilot run."""

    schema_version: str = "1.0"

    status: ReportStatus
    message: str

    repaired_csv_file_name: str | None = None

    audit_before: AuditReport
    repair_plan: RepairPlan | None = None
    policy_decision: PolicyDecision | None = None
    audit_after: AuditReport | None = None
    readiness_comparison: ReadinessComparison | None = None
    lineage_evidence: DatasetLineageEvidence | None = None


class ReportGenerationError(RuntimeError):
    """Raised when a machine-readable report cannot be generated."""


def build_machine_readable_report(
    *,
    status: ReportStatus,
    message: str,
    audit_before: AuditReport,
    repair_plan: RepairPlan | None = None,
    policy_decision: PolicyDecision | None = None,
    repaired_csv_path: str | Path | None = None,
    audit_after: AuditReport | None = None,
    readiness_comparison: ReadinessComparison | None = None,
    lineage_evidence: DatasetLineageEvidence | None = None,
) -> MachineReadableReport:
    """Build a structured evidence report without exposing dataset values."""

    repaired_csv_file_name: str | None = None

    if repaired_csv_path is not None:
        repaired_path = Path(repaired_csv_path)

        if not repaired_path.is_file():
            raise ReportGenerationError("The repaired CSV file does not exist.")

        repaired_csv_file_name = repaired_path.name

    if status == "REPAIRED":
        if repaired_csv_path is None:
            raise ReportGenerationError("A repaired run must include a repaired CSV file.")

        if audit_after is None:
            raise ReportGenerationError("A repaired run must include a post-repair audit.")

        if readiness_comparison is None:
            raise ReportGenerationError("A repaired run must include a readiness comparison.")

        if lineage_evidence is None:
            raise ReportGenerationError("A repaired run must include lineage evidence.")

    return MachineReadableReport(
        status=status,
        message=message,
        repaired_csv_file_name=repaired_csv_file_name,
        audit_before=audit_before,
        repair_plan=repair_plan,
        policy_decision=policy_decision,
        audit_after=audit_after,
        readiness_comparison=readiness_comparison,
        lineage_evidence=lineage_evidence,
    )


def write_machine_readable_report(
    report: MachineReadableReport,
    report_file: str | Path,
) -> Path:
    """Write a machine-readable JSON evidence report to disk."""

    report_path = Path(report_file)

    if report_path.suffix.lower() != ".json":
        raise ReportGenerationError("The machine-readable report must use a .json extension.")

    try:
        report_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        report_path.write_text(
            report.model_dump_json(
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        raise ReportGenerationError(f"Unable to write machine-readable report: {exc}") from exc

    return report_path
