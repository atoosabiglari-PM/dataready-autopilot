"""Tests for machine-readable DataReady evidence reporting."""

import json
from pathlib import Path

import pytest

from app.core.policy import (
    ActionDecision,
    PolicyDecision,
    ProposedRepair,
    RepairAction,
    RepairPlan,
)
from app.services.comparison import ReadinessComparison
from app.services.lineage import DatasetLineageEvidence
from app.services.reporting import (
    ReportGenerationError,
    build_machine_readable_report,
    write_machine_readable_report,
)
from app.tools.audit import AuditReport
from app.tools.preflight import PreflightReport


def make_preflight(file_name: str) -> PreflightReport:
    """Create a representative accepted preflight report."""

    return PreflightReport(
        status="ACCEPTED",
        file_name=file_name,
        file_size_bytes=1024,
        encoding="utf-8",
        delimiter=",",
        column_count=2,
        data_row_count=2,
        risk_flags=[],
        messages=[],
    )


def make_audit(
    *,
    file_name: str,
    fingerprint: str,
    status: str,
    quality_score: int,
) -> AuditReport:
    """Create a representative audit report."""

    return AuditReport(
        status=status,
        file_name=file_name,
        fingerprint_sha256=fingerprint,
        row_count=2,
        column_count=2,
        duplicate_row_count=0,
        quality_score=quality_score,
        preflight=make_preflight(file_name),
        issues=[],
    )


def make_repair_plan(source_fingerprint: str) -> RepairPlan:
    """Create a representative repair plan."""

    return RepairPlan(
        source_fingerprint_sha256=source_fingerprint,
        summary="Trim approved whitespace.",
        actions=[
            ProposedRepair(
                action=RepairAction.TRIM_OUTER_WHITESPACE,
                justification="Outer whitespace was detected.",
                columns=["name"],
            )
        ],
    )


def make_policy_decision(source_fingerprint: str) -> PolicyDecision:
    """Create a representative approved policy decision."""

    return PolicyDecision(
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


def make_comparison() -> ReadinessComparison:
    """Create representative before-and-after evidence."""

    return ReadinessComparison(
        before_status="QUARANTINED",
        after_status="READY",
        before_quality_score=90,
        after_quality_score=100,
        quality_score_delta=10,
        before_issue_count=1,
        after_issue_count=0,
        issue_count_delta=-1,
        before_row_count=2,
        after_row_count=2,
        row_count_delta=0,
        before_duplicate_row_count=0,
        after_duplicate_row_count=0,
        duplicate_rows_removed=0,
        resolved_issue_codes=["OUTER_WHITESPACE"],
        remaining_issue_codes=[],
        new_issue_codes=[],
    )


def make_lineage(
    source_fingerprint: str,
    output_fingerprint: str,
) -> DatasetLineageEvidence:
    """Create representative cryptographic lineage evidence."""

    return DatasetLineageEvidence(
        source_file_name="source.csv",
        output_file_name="repaired.csv",
        source_fingerprint_sha256=source_fingerprint,
        output_fingerprint_sha256=output_fingerprint,
        source_audit_status="QUARANTINED",
        output_audit_status="READY",
        policy_id="safe-auto-repairs",
        executed_actions=["TRIM_OUTER_WHITESPACE"],
        source_preserved=True,
    )


def test_builds_machine_readable_report_for_repaired_run(
    tmp_path: Path,
) -> None:
    source_fingerprint = "a" * 64
    output_fingerprint = "b" * 64

    repaired_csv = tmp_path / "repaired.csv"
    repaired_csv.write_text(
        "name,status\nAlice,active\nBob,inactive\n",
        encoding="utf-8",
    )

    before_audit = make_audit(
        file_name="source.csv",
        fingerprint=source_fingerprint,
        status="QUARANTINED",
        quality_score=90,
    )

    after_audit = make_audit(
        file_name="repaired.csv",
        fingerprint=output_fingerprint,
        status="READY",
        quality_score=100,
    )

    repair_plan = make_repair_plan(source_fingerprint)
    policy_decision = make_policy_decision(source_fingerprint)
    comparison = make_comparison()
    lineage = make_lineage(
        source_fingerprint,
        output_fingerprint,
    )

    report = build_machine_readable_report(
        status="REPAIRED",
        message="Repair completed.",
        audit_before=before_audit,
        repair_plan=repair_plan,
        policy_decision=policy_decision,
        repaired_csv_path=repaired_csv,
        audit_after=after_audit,
        readiness_comparison=comparison,
        lineage_evidence=lineage,
    )

    assert report.schema_version == "1.0"
    assert report.status == "REPAIRED"
    assert report.message == "Repair completed."
    assert report.repaired_csv_file_name == "repaired.csv"

    assert report.audit_before == before_audit
    assert report.repair_plan == repair_plan
    assert report.policy_decision == policy_decision
    assert report.audit_after == after_audit
    assert report.readiness_comparison == comparison
    assert report.lineage_evidence == lineage


def test_writes_valid_json_report(
    tmp_path: Path,
) -> None:
    source_fingerprint = "a" * 64
    output_fingerprint = "b" * 64

    repaired_csv = tmp_path / "repaired.csv"
    repaired_csv.write_text(
        "name,status\nAlice,active\n",
        encoding="utf-8",
    )

    report = build_machine_readable_report(
        status="REPAIRED",
        message="Repair completed.",
        audit_before=make_audit(
            file_name="source.csv",
            fingerprint=source_fingerprint,
            status="QUARANTINED",
            quality_score=90,
        ),
        repair_plan=make_repair_plan(source_fingerprint),
        policy_decision=make_policy_decision(source_fingerprint),
        repaired_csv_path=repaired_csv,
        audit_after=make_audit(
            file_name="repaired.csv",
            fingerprint=output_fingerprint,
            status="READY",
            quality_score=100,
        ),
        readiness_comparison=make_comparison(),
        lineage_evidence=make_lineage(
            source_fingerprint,
            output_fingerprint,
        ),
    )

    report_path = tmp_path / "repair-report.json"

    written_path = write_machine_readable_report(
        report,
        report_path,
    )

    assert written_path == report_path
    assert report_path.is_file()

    payload = json.loads(
        report_path.read_text(
            encoding="utf-8",
        )
    )

    assert payload["schema_version"] == "1.0"
    assert payload["status"] == "REPAIRED"
    assert payload["repaired_csv_file_name"] == "repaired.csv"

    assert payload["lineage_evidence"]["source_fingerprint_sha256"] == source_fingerprint
    assert payload["lineage_evidence"]["output_fingerprint_sha256"] == output_fingerprint


def test_repaired_report_requires_post_repair_evidence(
    tmp_path: Path,
) -> None:
    repaired_csv = tmp_path / "repaired.csv"
    repaired_csv.write_text(
        "name\nAlice\n",
        encoding="utf-8",
    )

    before_audit = make_audit(
        file_name="source.csv",
        fingerprint="a" * 64,
        status="QUARANTINED",
        quality_score=90,
    )

    with pytest.raises(
        ReportGenerationError,
        match="post-repair audit",
    ):
        build_machine_readable_report(
            status="REPAIRED",
            message="Repair completed.",
            audit_before=before_audit,
            repaired_csv_path=repaired_csv,
        )


def test_repaired_report_requires_existing_csv(
    tmp_path: Path,
) -> None:
    missing_csv = tmp_path / "missing.csv"

    with pytest.raises(
        ReportGenerationError,
        match="does not exist",
    ):
        build_machine_readable_report(
            status="REPAIRED",
            message="Repair completed.",
            audit_before=make_audit(
                file_name="source.csv",
                fingerprint="a" * 64,
                status="QUARANTINED",
                quality_score=90,
            ),
            repaired_csv_path=missing_csv,
            audit_after=make_audit(
                file_name="repaired.csv",
                fingerprint="b" * 64,
                status="READY",
                quality_score=100,
            ),
            readiness_comparison=make_comparison(),
            lineage_evidence=make_lineage(
                "a" * 64,
                "b" * 64,
            ),
        )


def test_report_writer_requires_json_extension(
    tmp_path: Path,
) -> None:
    report = build_machine_readable_report(
        status="READY",
        message="No repairs required.",
        audit_before=make_audit(
            file_name="source.csv",
            fingerprint="a" * 64,
            status="READY",
            quality_score=100,
        ),
    )

    with pytest.raises(
        ReportGenerationError,
        match=r"\.json extension",
    ):
        write_machine_readable_report(
            report,
            tmp_path / "report.txt",
        )
