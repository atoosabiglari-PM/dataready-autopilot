"""Tests for the safe Gemini planner-service boundary."""

import pytest

from app.core.policy import ProposedRepair, RepairAction, RepairPlan
from app.services.planner import (
    PlannerInputError,
    PlannerResponseError,
    _restore_column_names,
    build_planner_prompt,
    prepare_planner_input,
)
from app.tools.audit import AuditIssue, AuditReport
from app.tools.preflight import PreflightReport

FINGERPRINT = "a" * 64


def make_report(status: str = "QUARANTINED") -> AuditReport:
    """Build a representative deterministic audit report."""
    preflight = PreflightReport(
        status="ACCEPTED",
        file_name="private_customers.csv",
        file_size_bytes=2048,
        encoding="utf-8",
        delimiter=",",
        column_count=2,
        data_row_count=10,
        risk_flags=["private preflight risk"],
        messages=["private preflight message"],
    )

    return AuditReport(
        status=status,
        file_name="private_customers.csv",
        fingerprint_sha256=FINGERPRINT,
        row_count=10,
        column_count=2,
        duplicate_row_count=1,
        quality_score=80,
        preflight=preflight,
        issues=[
            AuditIssue(
                code="OUTER_WHITESPACE",
                severity="WARNING",
                message="Sensitive Name contains whitespace",
                column="Sensitive Name",
                count=3,
            ),
            AuditIssue(
                code="PII_DETECTED",
                severity="CRITICAL",
                message="Secret Email contains private values",
                column="Secret Email",
                count=2,
            ),
        ],
    )


def test_prepare_planner_input_removes_dataset_derived_text() -> None:
    """Only minimized evidence and opaque column aliases may reach Gemini."""
    prepared = prepare_planner_input(make_report())
    serialized = prepared.evidence.model_dump_json()

    assert prepared.alias_to_column == {
        "column_001": "Secret Email",
        "column_002": "Sensitive Name",
    }
    assert prepared.evidence.source_fingerprint_sha256 == FINGERPRINT
    assert [issue.column_ref for issue in prepared.evidence.issues] == [
        "column_002",
        "column_001",
    ]

    forbidden_text = (
        "private_customers.csv",
        "Sensitive Name",
        "Secret Email",
        "private preflight risk",
        "private preflight message",
        "contains whitespace",
        "contains private values",
    )
    for text in forbidden_text:
        assert text not in serialized


def test_build_planner_prompt_contains_only_sanitized_evidence() -> None:
    """The prompt must not restore local-only dataset names."""
    prepared = prepare_planner_input(make_report())
    prompt = build_planner_prompt(prepared.evidence)

    assert FINGERPRINT in prompt
    assert "column_001" in prompt
    assert "column_002" in prompt
    assert "Secret Email" not in prompt
    assert "Sensitive Name" not in prompt
    assert "private_customers.csv" not in prompt


def test_blocked_report_is_rejected_before_model_use() -> None:
    """A blocked audit report must never be prepared for Gemini."""
    with pytest.raises(
        PlannerInputError,
        match="BLOCKED audit reports must not be sent to Gemini",
    ):
        prepare_planner_input(make_report(status="BLOCKED"))


def test_restore_column_names_uses_only_known_local_aliases() -> None:
    """Valid aliases are restored, while invented aliases are rejected."""
    plan = RepairPlan(
        source_fingerprint_sha256=FINGERPRINT,
        summary="Whitespace may be safely trimmed.",
        actions=[
            ProposedRepair(
                action=RepairAction.TRIM_OUTER_WHITESPACE,
                justification="Deterministic whitespace evidence exists.",
                columns=["column_002"],
            )
        ],
    )

    restored = _restore_column_names(
        plan,
        {"column_002": "Sensitive Name"},
    )
    assert restored.actions[0].columns == ["Sensitive Name"]

    unknown_alias_plan = plan.model_copy(
        update={"actions": [plan.actions[0].model_copy(update={"columns": ["column_999"]})]}
    )
    with pytest.raises(
        PlannerResponseError,
        match="unknown column alias",
    ):
        _restore_column_names(
            unknown_alias_plan,
            {"column_002": "Sensitive Name"},
        )
