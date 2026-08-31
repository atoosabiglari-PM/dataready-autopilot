"""Tests for the deterministic Gemini-to-policy authorization bridge."""

import asyncio

import pytest

from app.core.policy import (
    DatasetPolicy,
    ProposedRepair,
    RepairAction,
    RepairPlan,
)
from app.services.authorization import (
    authorize_repair_plan,
    propose_and_authorize_repair,
)
from app.tools.audit import AuditIssue, AuditReport
from app.tools.preflight import PreflightReport

FINGERPRINT = "a" * 64


def make_audit_report(
    *,
    issues: list[AuditIssue] | None = None,
) -> AuditReport:
    """Create a representative deterministic audit report."""

    return AuditReport(
        status="QUARANTINED",
        file_name="customers.csv",
        fingerprint_sha256=FINGERPRINT,
        row_count=10,
        column_count=2,
        duplicate_row_count=0,
        quality_score=90,
        preflight=PreflightReport(
            status="ACCEPTED",
            file_name="customers.csv",
            file_size_bytes=1024,
            encoding="utf-8",
            delimiter=",",
            column_count=2,
            data_row_count=10,
            risk_flags=[],
            messages=[],
        ),
        issues=issues or [],
    )


def make_trim_plan() -> RepairPlan:
    """Create one constrained Gemini-style repair plan."""

    return RepairPlan(
        source_fingerprint_sha256=FINGERPRINT,
        summary="Trim whitespace.",
        actions=[
            ProposedRepair(
                action=RepairAction.TRIM_OUTER_WHITESPACE,
                justification="Whitespace was detected.",
                columns=["name"],
            )
        ],
    )


def test_authorizes_explicitly_allowed_gemini_action() -> None:
    audit_report = make_audit_report()
    repair_plan = make_trim_plan()

    policy = DatasetPolicy(
        policy_id="safe-auto-repairs",
        allowed_actions={
            RepairAction.TRIM_OUTER_WHITESPACE,
        },
    )

    result = authorize_repair_plan(
        audit_report,
        repair_plan,
        policy,
    )

    assert result.repair_plan == repair_plan
    assert result.policy_decision.status == "APPROVED"
    assert result.policy_decision.can_execute is True
    assert result.policy_decision.action_decisions[0].decision == "APPROVED"


def test_gemini_proposal_requires_review_when_policy_does_not_allow_it() -> None:
    audit_report = make_audit_report()
    repair_plan = make_trim_plan()

    policy = DatasetPolicy(
        policy_id="deny-by-default",
        allowed_actions=set(),
    )

    result = authorize_repair_plan(
        audit_report,
        repair_plan,
        policy,
    )

    assert result.repair_plan == repair_plan
    assert result.policy_decision.status == "REQUIRES_REVIEW"
    assert result.policy_decision.can_execute is False
    assert result.policy_decision.action_decisions[0].decision == "REQUIRES_REVIEW"


def test_critical_audit_finding_overrides_allowed_action() -> None:
    audit_report = make_audit_report(
        issues=[
            AuditIssue(
                code="PII_VALUE_PATTERN",
                severity="CRITICAL",
                message="Sensitive values detected.",
                column="name",
                count=1,
            )
        ]
    )

    repair_plan = make_trim_plan()

    policy = DatasetPolicy(
        policy_id="safe-auto-repairs",
        allowed_actions={
            RepairAction.TRIM_OUTER_WHITESPACE,
        },
    )

    result = authorize_repair_plan(
        audit_report,
        repair_plan,
        policy,
    )

    assert result.policy_decision.status == "REQUIRES_REVIEW"
    assert result.policy_decision.can_execute is False
    assert result.policy_decision.action_decisions[0].decision == "REQUIRES_REVIEW"


def test_gemini_cannot_authorize_its_own_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_report = make_audit_report()
    gemini_plan = make_trim_plan()

    async def fake_propose_repair_plan(
        report: AuditReport,
    ) -> RepairPlan:
        assert report == audit_report
        return gemini_plan

    monkeypatch.setattr(
        "app.services.authorization.propose_repair_plan",
        fake_propose_repair_plan,
    )

    policy = DatasetPolicy(
        policy_id="deny-by-default",
        allowed_actions=set(),
    )

    result = asyncio.run(
        propose_and_authorize_repair(
            audit_report,
            policy,
        )
    )

    # Gemini successfully proposed the action.
    assert result.repair_plan == gemini_plan

    # Independent deterministic policy still refuses automatic execution.
    assert result.policy_decision.status == "REQUIRES_REVIEW"
    assert result.policy_decision.can_execute is False
