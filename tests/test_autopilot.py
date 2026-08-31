"""Tests for the governed end-to-end DataReady Autopilot workflow."""

import asyncio
from pathlib import Path

import pandas as pd
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
    *,
    status: str = "ACCEPTED",
) -> PreflightReport:
    """Create a representative preflight result."""

    return PreflightReport(
        status=status,
        file_name=file_name,
        file_size_bytes=1024,
        encoding="utf-8",
        delimiter=",",
        column_count=2,
        data_row_count=2,
        risk_flags=[] if status == "ACCEPTED" else ["TEST_BLOCK"],
        messages=[] if status == "ACCEPTED" else ["Blocked for testing."],
    )


def make_audit(
    source: Path,
    *,
    status: str,
    issues: list[AuditIssue] | None = None,
) -> AuditReport:
    """Create an audit report bound to the supplied source file."""

    return AuditReport(
        status=status,
        file_name=source.name,
        fingerprint_sha256=calculate_sha256(source),
        row_count=2 if status != "BLOCKED" else 0,
        column_count=2 if status != "BLOCKED" else 0,
        duplicate_row_count=0,
        quality_score=(100 if status == "READY" else 90 if status == "QUARANTINED" else 0),
        preflight=make_preflight(
            source.name,
            status="BLOCKED" if status == "BLOCKED" else "ACCEPTED",
        ),
        issues=issues or [],
    )


def make_trim_plan(fingerprint: str) -> RepairPlan:
    """Create an approved-style whitespace repair plan."""

    return RepairPlan(
        source_fingerprint_sha256=fingerprint,
        summary="Trim approved whitespace.",
        actions=[
            ProposedRepair(
                action=RepairAction.TRIM_OUTER_WHITESPACE,
                justification="Outer whitespace was detected.",
                columns=["name"],
            )
        ],
    )


def test_blocked_dataset_never_calls_gemini(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "blocked.csv"
    source.write_text(
        "name,status\nAlice,active\n",
        encoding="utf-8",
    )

    audit_report = make_audit(
        source,
        status="BLOCKED",
        issues=[
            AuditIssue(
                code="TEST_BLOCK",
                severity="CRITICAL",
                message="Blocked for testing.",
                count=1,
            )
        ],
    )

    monkeypatch.setattr(
        "app.services.autopilot.audit_csv",
        lambda path: audit_report,
    )

    async def forbidden_gemini_call(
        *args: object,
        **kwargs: object,
    ) -> object:
        raise AssertionError("Gemini must not be called for blocked data.")

    monkeypatch.setattr(
        "app.services.autopilot.propose_and_authorize_repair",
        forbidden_gemini_call,
    )

    output = tmp_path / "repaired.csv"

    result = asyncio.run(
        run_autopilot(
            source,
            output,
        )
    )

    assert result.status == "BLOCKED"
    assert result.repair_plan is None
    assert result.policy_decision is None
    assert result.output_path is None
    assert result.post_repair_audit is None
    assert result.readiness_comparison is None
    assert result.lineage_evidence is None
    assert not output.exists()


def test_ready_dataset_skips_gemini_and_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "ready.csv"
    source.write_text(
        "name,status\nAlice,active\nBob,inactive\n",
        encoding="utf-8",
    )

    audit_report = make_audit(
        source,
        status="READY",
    )

    monkeypatch.setattr(
        "app.services.autopilot.audit_csv",
        lambda path: audit_report,
    )

    async def forbidden_gemini_call(
        *args: object,
        **kwargs: object,
    ) -> object:
        raise AssertionError("Gemini must not be called for already-ready data.")

    monkeypatch.setattr(
        "app.services.autopilot.propose_and_authorize_repair",
        forbidden_gemini_call,
    )

    output = tmp_path / "repaired.csv"

    result = asyncio.run(
        run_autopilot(
            source,
            output,
        )
    )

    assert result.status == "READY"
    assert result.repair_plan is None
    assert result.policy_decision is None
    assert result.output_path is None
    assert result.post_repair_audit is None
    assert result.readiness_comparison is None
    assert result.lineage_evidence is None
    assert not output.exists()


def test_review_required_plan_is_not_executed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "review.csv"
    source.write_text(
        "name,status\n Alice ,active\n Bob ,inactive\n",
        encoding="utf-8",
    )

    audit_report = make_audit(
        source,
        status="QUARANTINED",
        issues=[
            AuditIssue(
                code="OUTER_WHITESPACE",
                severity="WARNING",
                message="Whitespace detected.",
                column="name",
                count=2,
            )
        ],
    )

    fingerprint = calculate_sha256(source)
    repair_plan = make_trim_plan(fingerprint)

    proposal = AuthorizedRepairProposal(
        repair_plan=repair_plan,
        policy_decision=PolicyDecision(
            status="REQUIRES_REVIEW",
            can_execute=False,
            policy_id="review-policy",
            source_fingerprint_sha256=fingerprint,
            action_decisions=[
                ActionDecision(
                    action=RepairAction.TRIM_OUTER_WHITESPACE,
                    decision="REQUIRES_REVIEW",
                    reason="Human review required.",
                    columns=["name"],
                )
            ],
            reasons=["Human review required."],
        ),
    )

    monkeypatch.setattr(
        "app.services.autopilot.audit_csv",
        lambda path: audit_report,
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

    output = tmp_path / "repaired.csv"

    result = asyncio.run(
        run_autopilot(
            source,
            output,
        )
    )

    assert result.status == "REQUIRES_REVIEW"
    assert result.repair_plan == repair_plan
    assert result.policy_decision == proposal.policy_decision
    assert result.output_path is None
    assert result.post_repair_audit is None
    assert result.readiness_comparison is None
    assert result.lineage_evidence is None
    assert not output.exists()


def test_denied_plan_is_not_executed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "denied.csv"
    source.write_text(
        "name,status\n Alice ,active\n Bob ,inactive\n",
        encoding="utf-8",
    )

    audit_report = make_audit(
        source,
        status="QUARANTINED",
    )

    fingerprint = calculate_sha256(source)
    repair_plan = make_trim_plan(fingerprint)

    proposal = AuthorizedRepairProposal(
        repair_plan=repair_plan,
        policy_decision=PolicyDecision(
            status="DENIED",
            can_execute=False,
            policy_id="deny-policy",
            source_fingerprint_sha256=fingerprint,
            action_decisions=[
                ActionDecision(
                    action=RepairAction.TRIM_OUTER_WHITESPACE,
                    decision="DENIED",
                    reason="Denied for testing.",
                    columns=["name"],
                )
            ],
            reasons=["Denied."],
        ),
    )

    monkeypatch.setattr(
        "app.services.autopilot.audit_csv",
        lambda path: audit_report,
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

    output = tmp_path / "repaired.csv"

    result = asyncio.run(
        run_autopilot(
            source,
            output,
        )
    )

    assert result.status == "DENIED"
    assert result.output_path is None
    assert result.post_repair_audit is None
    assert result.readiness_comparison is None
    assert result.lineage_evidence is None
    assert not output.exists()


def test_approved_plan_executes_on_separate_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.csv"
    source.write_text(
        "name,status\n Alice ,active\n Bob ,inactive\n",
        encoding="utf-8",
    )

    original_bytes = source.read_bytes()

    audit_report = make_audit(
        source,
        status="QUARANTINED",
        issues=[
            AuditIssue(
                code="OUTER_WHITESPACE",
                severity="WARNING",
                message="Whitespace detected.",
                column="name",
                count=2,
            )
        ],
    )

    fingerprint = calculate_sha256(source)
    repair_plan = make_trim_plan(fingerprint)

    proposal = AuthorizedRepairProposal(
        repair_plan=repair_plan,
        policy_decision=PolicyDecision(
            status="APPROVED",
            can_execute=True,
            policy_id="safe-auto-repairs",
            source_fingerprint_sha256=fingerprint,
            action_decisions=[
                ActionDecision(
                    action=RepairAction.TRIM_OUTER_WHITESPACE,
                    decision="APPROVED",
                    reason="Explicitly authorized.",
                    columns=["name"],
                )
            ],
            reasons=["Approved."],
        ),
    )

    monkeypatch.setattr(
        "app.services.autopilot.audit_csv",
        lambda path: (
            audit_report
            if Path(path) == source
            else make_audit(
                Path(path),
                status="READY",
            )
        ),
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

    output = tmp_path / "repaired.csv"

    result = asyncio.run(
        run_autopilot(
            source,
            output,
        )
    )

    assert result.status == "REPAIRED"
    assert result.output_path == output
    assert output.is_file()

    assert source.read_bytes() == original_bytes
    assert calculate_sha256(source) == fingerprint

    repaired = pd.read_csv(
        output,
        dtype=str,
        keep_default_na=False,
    )

    assert repaired["name"].tolist() == ["Alice", "Bob"]
    assert repaired["status"].tolist() == ["active", "inactive"]

    assert result.post_repair_audit is not None
    assert result.post_repair_audit.fingerprint_sha256 == calculate_sha256(output)

    assert result.readiness_comparison is not None
    assert result.readiness_comparison.before_status == "QUARANTINED"
    assert result.readiness_comparison.after_status == "READY"

    assert result.lineage_evidence is not None
    assert result.lineage_evidence.source_fingerprint_sha256 == fingerprint
    assert result.lineage_evidence.output_fingerprint_sha256 == calculate_sha256(output)
    assert result.lineage_evidence.source_preserved is True
    assert result.lineage_evidence.policy_id == "safe-auto-repairs"
    assert result.lineage_evidence.executed_actions == [
        "TRIM_OUTER_WHITESPACE",
    ]


def test_repaired_output_is_automatically_reaudited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.csv"
    source.write_text(
        "name,status\n Alice ,active\n Bob ,inactive\n",
        encoding="utf-8",
    )

    original_audit = make_audit(
        source,
        status="QUARANTINED",
        issues=[
            AuditIssue(
                code="OUTER_WHITESPACE",
                severity="WARNING",
                message="Whitespace detected.",
                column="name",
                count=2,
            )
        ],
    )

    fingerprint = calculate_sha256(source)
    repair_plan = make_trim_plan(fingerprint)

    proposal = AuthorizedRepairProposal(
        repair_plan=repair_plan,
        policy_decision=PolicyDecision(
            status="APPROVED",
            can_execute=True,
            policy_id="safe-auto-repairs",
            source_fingerprint_sha256=fingerprint,
            action_decisions=[
                ActionDecision(
                    action=RepairAction.TRIM_OUTER_WHITESPACE,
                    decision="APPROVED",
                    reason="Explicitly authorized.",
                    columns=["name"],
                )
            ],
            reasons=["Approved."],
        ),
    )

    monkeypatch.setattr(
        "app.services.autopilot.audit_csv",
        lambda path: (
            original_audit
            if Path(path) == source
            else make_audit(
                Path(path),
                status="READY",
            )
        ),
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

    output = tmp_path / "repaired.csv"

    result = asyncio.run(
        run_autopilot(
            source,
            output,
        )
    )

    assert result.status == "REPAIRED"
    assert result.output_path == output

    assert result.post_repair_audit is not None
    assert result.post_repair_audit.status == "READY"
    assert result.post_repair_audit.file_name == output.name
    assert result.post_repair_audit.fingerprint_sha256 == calculate_sha256(output)

    assert result.readiness_comparison is not None
    assert result.readiness_comparison.before_status == "QUARANTINED"
    assert result.readiness_comparison.after_status == "READY"

    assert result.lineage_evidence is not None
    assert result.lineage_evidence.source_fingerprint_sha256 == fingerprint
    assert result.lineage_evidence.output_fingerprint_sha256 == calculate_sha256(output)
    assert result.lineage_evidence.source_preserved is True
