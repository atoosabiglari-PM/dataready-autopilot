"""Cryptographic lineage evidence for governed DataReady repairs."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from app.core.policy import PolicyDecision, RepairPlan
from app.tools.audit import AuditReport
from app.tools.fingerprint import calculate_sha256


class LineageEvidenceError(RuntimeError):
    """Raised when repair lineage cannot be proven."""


class DatasetLineageEvidence(BaseModel):
    """Cryptographic evidence connecting source, repair, and output."""

    source_file_name: str
    output_file_name: str

    source_fingerprint_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    output_fingerprint_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )

    source_audit_status: str
    output_audit_status: str

    policy_id: str
    executed_actions: list[str] = Field(default_factory=list)

    source_preserved: bool


def build_lineage_evidence(
    source_file: str | Path,
    output_file: str | Path,
    before_audit: AuditReport,
    after_audit: AuditReport,
    repair_plan: RepairPlan,
    policy_decision: PolicyDecision,
) -> DatasetLineageEvidence:
    """Verify and record cryptographic lineage for one repaired dataset."""

    source_path = Path(source_file)
    output_path = Path(output_file)

    if not source_path.is_file():
        raise LineageEvidenceError("The original source file does not exist.")

    if not output_path.is_file():
        raise LineageEvidenceError("The repaired output file does not exist.")

    source_fingerprint = calculate_sha256(source_path)
    output_fingerprint = calculate_sha256(output_path)

    if before_audit.fingerprint_sha256 != source_fingerprint:
        raise LineageEvidenceError("The source fingerprint does not match the original audit.")

    if repair_plan.source_fingerprint_sha256 != source_fingerprint:
        raise LineageEvidenceError("The repair plan is not bound to the current source file.")

    if policy_decision.source_fingerprint_sha256 != source_fingerprint:
        raise LineageEvidenceError("The policy decision is not bound to the current source file.")

    if after_audit.fingerprint_sha256 != output_fingerprint:
        raise LineageEvidenceError("The output fingerprint does not match the post-repair audit.")

    source_preserved = calculate_sha256(source_path) == source_fingerprint

    if not source_preserved:
        raise LineageEvidenceError(
            "The original source file changed while lineage evidence was being created."
        )

    return DatasetLineageEvidence(
        source_file_name=source_path.name,
        output_file_name=output_path.name,
        source_fingerprint_sha256=source_fingerprint,
        output_fingerprint_sha256=output_fingerprint,
        source_audit_status=before_audit.status,
        output_audit_status=after_audit.status,
        policy_id=policy_decision.policy_id,
        executed_actions=[action.action.value for action in repair_plan.actions],
        source_preserved=True,
    )
