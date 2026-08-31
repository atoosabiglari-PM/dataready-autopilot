"""Tests for cryptographic repair-lineage evidence."""

from pathlib import Path

import pytest

from app.core.policy import (
    ActionDecision,
    PolicyDecision,
    ProposedRepair,
    RepairAction,
    RepairPlan,
)
from app.services.lineage import (
    LineageEvidenceError,
    build_lineage_evidence,
)
from app.tools.audit import AuditReport
from app.tools.fingerprint import calculate_sha256
from app.tools.preflight import PreflightReport


def make_preflight(file_name: str) -> PreflightReport:
    """Create a representative accepted preflight report."""

    return PreflightReport(
        status="ACCEPTED",
        file_name=file_name,
        file_size_bytes=1024,
        encoding="utf-8",
        delimiter=",",
        column_count=1,
        data_row_count=1,
        risk_flags=[],
        messages=[],
    )


def make_audit(
    path: Path,
    *,
    status: str,
) -> AuditReport:
    """Create an audit report bound to the supplied file."""

    return AuditReport(
        status=status,
        file_name=path.name,
        fingerprint_sha256=calculate_sha256(path),
        row_count=1,
        column_count=1,
        duplicate_row_count=0,
        quality_score=100 if status == "READY" else 90,
        preflight=make_preflight(path.name),
        issues=[],
    )


def test_builds_valid_source_to_output_lineage(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text(
        "name\n Alice \n",
        encoding="utf-8",
    )

    output = tmp_path / "repaired.csv"
    output.write_text(
        "name\nAlice\n",
        encoding="utf-8",
    )

    source_fingerprint = calculate_sha256(source)

    before_audit = make_audit(
        source,
        status="QUARANTINED",
    )

    after_audit = make_audit(
        output,
        status="READY",
    )

    repair_plan = RepairPlan(
        source_fingerprint_sha256=source_fingerprint,
        summary="Trim whitespace.",
        actions=[
            ProposedRepair(
                action=RepairAction.TRIM_OUTER_WHITESPACE,
                justification="Whitespace was detected.",
                columns=["name"],
            )
        ],
    )

    policy_decision = PolicyDecision(
        status="APPROVED",
        can_execute=True,
        policy_id="safe-auto-repairs",
        source_fingerprint_sha256=source_fingerprint,
        action_decisions=[
            ActionDecision(
                action=RepairAction.TRIM_OUTER_WHITESPACE,
                decision="APPROVED",
                reason="Explicitly authorized.",
                columns=["name"],
            )
        ],
        reasons=["Approved."],
    )

    evidence = build_lineage_evidence(
        source,
        output,
        before_audit,
        after_audit,
        repair_plan,
        policy_decision,
    )

    assert evidence.source_file_name == "source.csv"
    assert evidence.output_file_name == "repaired.csv"

    assert evidence.source_fingerprint_sha256 == calculate_sha256(source)
    assert evidence.output_fingerprint_sha256 == calculate_sha256(output)

    assert evidence.source_audit_status == "QUARANTINED"
    assert evidence.output_audit_status == "READY"

    assert evidence.policy_id == "safe-auto-repairs"
    assert evidence.executed_actions == [
        "TRIM_OUTER_WHITESPACE",
    ]

    assert evidence.source_preserved is True


def test_rejects_output_that_does_not_match_post_repair_audit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.csv"
    source.write_text(
        "name\n Alice \n",
        encoding="utf-8",
    )

    output = tmp_path / "repaired.csv"
    output.write_text(
        "name\nAlice\n",
        encoding="utf-8",
    )

    source_fingerprint = calculate_sha256(source)

    before_audit = make_audit(
        source,
        status="QUARANTINED",
    )

    after_audit = make_audit(
        output,
        status="READY",
    )

    repair_plan = RepairPlan(
        source_fingerprint_sha256=source_fingerprint,
        summary="Trim whitespace.",
        actions=[
            ProposedRepair(
                action=RepairAction.TRIM_OUTER_WHITESPACE,
                justification="Whitespace was detected.",
                columns=["name"],
            )
        ],
    )

    policy_decision = PolicyDecision(
        status="APPROVED",
        can_execute=True,
        policy_id="safe-auto-repairs",
        source_fingerprint_sha256=source_fingerprint,
        action_decisions=[
            ActionDecision(
                action=RepairAction.TRIM_OUTER_WHITESPACE,
                decision="APPROVED",
                reason="Explicitly authorized.",
                columns=["name"],
            )
        ],
        reasons=["Approved."],
    )

    # Change the output after its audit was created.
    output.write_text(
        "name\nMallory\n",
        encoding="utf-8",
    )

    with pytest.raises(
        LineageEvidenceError,
        match="output fingerprint does not match",
    ):
        build_lineage_evidence(
            source,
            output,
            before_audit,
            after_audit,
            repair_plan,
            policy_decision,
        )
