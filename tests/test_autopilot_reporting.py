"""Integration test for Autopilot repaired CSV and JSON evidence output."""

import asyncio
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
from app.services.authorization import AuthorizedRepairProposal
from app.services.autopilot import run_autopilot
from app.tools.audit import AuditIssue, AuditReport
from app.tools.fingerprint import calculate_sha256
from app.tools.preflight import PreflightReport


def make_preflight(
    file_name: str,
) -> PreflightReport:
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
    path: Path,
    *,
    status: str,
    issues: list[AuditIssue] | None = None,
) -> AuditReport:
    """Create an audit report bound to the supplied file."""

    return AuditReport(
        status=status,
        file_name=path.name,
        fingerprint_sha256=calculate_sha256(path),
        row_count=2,
        column_count=2,
        duplicate_row_count=0,
        quality_score=100 if status == "READY" else 90,
        preflight=make_preflight(path.name),
        issues=issues or [],
    )


def test_repaired_run_creates_csv_and_json_evidence_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful repair must create both governed output artifacts."""

    source = tmp_path / "source.csv"
    source.write_text(
        "name,status\n Alice ,active\n Bob ,inactive\n",
        encoding="utf-8",
    )

    source_fingerprint = calculate_sha256(source)

    before_audit = make_audit(
        source,
        status="QUARANTINED",
        issues=[
            AuditIssue(
                code="OUTER_WHITESPACE",
                severity="WARNING",
                message="Outer whitespace detected.",
                column="name",
                count=2,
            )
        ],
    )

    repair_plan = RepairPlan(
        source_fingerprint_sha256=source_fingerprint,
        summary="Trim approved outer whitespace.",
        actions=[
            ProposedRepair(
                action=RepairAction.TRIM_OUTER_WHITESPACE,
                justification="Outer whitespace was detected.",
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

    proposal = AuthorizedRepairProposal(
        repair_plan=repair_plan,
        policy_decision=policy_decision,
    )

    output = tmp_path / "repaired.csv"

    def fake_audit(path: str | Path) -> AuditReport:
        audit_path = Path(path)

        if audit_path == source:
            return before_audit

        return make_audit(
            audit_path,
            status="READY",
        )

    monkeypatch.setattr(
        "app.services.autopilot.audit_csv",
        fake_audit,
    )

    async def fake_proposal(
        *args: object,
        **kwargs: object,
    ) -> AuthorizedRepairProposal:
        return proposal

    monkeypatch.setattr(
        "app.services.autopilot.propose_and_authorize_repair",
        fake_proposal,
    )

    result = asyncio.run(
        run_autopilot(
            source,
            output,
        )
    )

    expected_report_path = tmp_path / "repaired-report.json"

    assert result.status == "REPAIRED"

    assert result.output_path == output
    assert result.output_path.is_file()

    assert result.report_path == expected_report_path
    assert result.report_path.is_file()

    assert result.machine_readable_report is not None
    assert result.post_repair_audit is not None
    assert result.readiness_comparison is not None
    assert result.lineage_evidence is not None

    assert calculate_sha256(source) == source_fingerprint

    output_fingerprint = calculate_sha256(output)

    assert result.lineage_evidence.source_fingerprint_sha256 == source_fingerprint
    assert result.lineage_evidence.output_fingerprint_sha256 == output_fingerprint
    assert result.lineage_evidence.source_preserved is True

    payload = json.loads(
        expected_report_path.read_text(
            encoding="utf-8",
        )
    )

    assert payload["schema_version"] == "1.0"
    assert payload["status"] == "REPAIRED"
    assert payload["repaired_csv_file_name"] == "repaired.csv"

    assert payload["audit_before"]["fingerprint_sha256"] == source_fingerprint
    assert payload["audit_after"]["fingerprint_sha256"] == output_fingerprint

    assert payload["repair_plan"]["source_fingerprint_sha256"] == source_fingerprint
    assert payload["policy_decision"]["source_fingerprint_sha256"] == source_fingerprint

    assert payload["lineage_evidence"]["source_fingerprint_sha256"] == source_fingerprint
    assert payload["lineage_evidence"]["output_fingerprint_sha256"] == output_fingerprint

    assert payload["lineage_evidence"]["source_preserved"] is True
    assert payload["lineage_evidence"]["executed_actions"] == [
        "TRIM_OUTER_WHITESPACE",
    ]

    assert payload["readiness_comparison"]["before_status"] == "QUARANTINED"
    assert payload["readiness_comparison"]["after_status"] == "READY"
