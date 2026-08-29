"""Tests for deterministic repair-plan authorization."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.policy import (
    DatasetPolicy,
    ProposedRepair,
    RepairAction,
    RepairPlan,
    validate_repair_plan,
)
from app.tools.audit import AuditReport, audit_csv


def _repair_plan(
    audit_report: AuditReport,
    action: RepairAction | None = None,
) -> RepairPlan:
    """Create a repair plan bound to an audited file."""

    assert audit_report.fingerprint_sha256 is not None

    actions = []

    if action is not None:
        actions.append(
            ProposedRepair(
                action=action,
                justification="Resolve a deterministic audit finding.",
            )
        )

    return RepairPlan(
        source_fingerprint_sha256=audit_report.fingerprint_sha256,
        summary="A constrained repair proposal.",
        actions=actions,
    )


def test_ready_dataset_with_no_repairs_is_approved(
    tmp_path: Path,
) -> None:
    csv_file = tmp_path / "ready.csv"
    csv_file.write_text(
        "product,quantity\nwidget,2\ngadget,3\n",
        encoding="utf-8",
    )
    audit_report = audit_csv(csv_file)
    repair_plan = _repair_plan(audit_report)

    decision = validate_repair_plan(audit_report, repair_plan)

    assert decision.status == "APPROVED"
    assert decision.can_execute is True
    assert decision.action_decisions == []


def test_conservative_default_requires_review_for_repair(
    tmp_path: Path,
) -> None:
    csv_file = tmp_path / "duplicates.csv"
    csv_file.write_text(
        "product,quantity\nwidget,2\nwidget,2\n",
        encoding="utf-8",
    )
    audit_report = audit_csv(csv_file)
    repair_plan = _repair_plan(
        audit_report,
        RepairAction.REMOVE_EXACT_DUPLICATES,
    )

    decision = validate_repair_plan(audit_report, repair_plan)

    assert decision.status == "REQUIRES_REVIEW"
    assert decision.can_execute is False
    assert decision.action_decisions[0].decision == "REQUIRES_REVIEW"


def test_explicit_dataset_permission_approves_action(
    tmp_path: Path,
) -> None:
    csv_file = tmp_path / "approved_duplicates.csv"
    csv_file.write_text(
        "product,quantity\nwidget,2\nwidget,2\n",
        encoding="utf-8",
    )
    audit_report = audit_csv(csv_file)
    repair_plan = _repair_plan(
        audit_report,
        RepairAction.REMOVE_EXACT_DUPLICATES,
    )
    dataset_policy = DatasetPolicy(
        policy_id="explicit-duplicate-policy",
        allowed_actions={
            RepairAction.REMOVE_EXACT_DUPLICATES,
        },
    )

    decision = validate_repair_plan(
        audit_report,
        repair_plan,
        dataset_policy,
    )

    assert decision.status == "APPROVED"
    assert decision.can_execute is True
    assert decision.action_decisions[0].decision == "APPROVED"


def test_fingerprint_mismatch_is_denied(tmp_path: Path) -> None:
    csv_file = tmp_path / "fingerprint.csv"
    csv_file.write_text(
        "product,quantity\nwidget,2\n",
        encoding="utf-8",
    )
    audit_report = audit_csv(csv_file)
    repair_plan = RepairPlan(
        source_fingerprint_sha256="0" * 64,
        summary="A plan bound to a different file.",
        actions=[
            ProposedRepair(
                action=RepairAction.TRIM_OUTER_WHITESPACE,
                justification="Normalize whitespace.",
            )
        ],
    )

    decision = validate_repair_plan(audit_report, repair_plan)

    assert decision.status == "DENIED"
    assert decision.can_execute is False
    assert "fingerprint" in decision.reasons[0].lower()


def test_blocked_file_cannot_enter_repair_pipeline(
    tmp_path: Path,
) -> None:
    csv_file = tmp_path / "header_only.csv"
    csv_file.write_text("product,quantity\n", encoding="utf-8")
    audit_report = audit_csv(csv_file)
    repair_plan = _repair_plan(
        audit_report,
        RepairAction.REMOVE_EXACT_DUPLICATES,
    )

    decision = validate_repair_plan(audit_report, repair_plan)

    assert audit_report.status == "BLOCKED"
    assert decision.status == "DENIED"
    assert decision.can_execute is False


def test_critical_finding_requires_review_even_if_action_is_allowed(
    tmp_path: Path,
) -> None:
    csv_file = tmp_path / "pii.csv"
    csv_file.write_text(
        "email,department\nperson@example.com,operations\n",
        encoding="utf-8",
    )
    audit_report = audit_csv(csv_file)
    repair_plan = _repair_plan(
        audit_report,
        RepairAction.REDACT_PII,
    )
    dataset_policy = DatasetPolicy(
        policy_id="pii-redaction-policy",
        allowed_actions={RepairAction.REDACT_PII},
    )

    decision = validate_repair_plan(
        audit_report,
        repair_plan,
        dataset_policy,
    )

    assert decision.status == "REQUIRES_REVIEW"
    assert decision.can_execute is False
    assert "PII" in decision.reasons[0]


def test_arbitrary_agent_action_is_rejected_by_schema(
    tmp_path: Path,
) -> None:
    csv_file = tmp_path / "schema.csv"
    csv_file.write_text(
        "product,quantity\nwidget,2\n",
        encoding="utf-8",
    )
    audit_report = audit_csv(csv_file)

    assert audit_report.fingerprint_sha256 is not None

    with pytest.raises(ValidationError):
        RepairPlan.model_validate(
            {
                "source_fingerprint_sha256": (audit_report.fingerprint_sha256),
                "summary": "Attempt to execute an unsupported action.",
                "actions": [
                    {
                        "action": "RUN_ARBITRARY_PYTHON",
                        "justification": "The AI requested it.",
                    }
                ],
            }
        )


def test_policy_validation_does_not_modify_source(
    tmp_path: Path,
) -> None:
    csv_file = tmp_path / "preserved.csv"
    original_content = b"product,quantity\nwidget,2\nwidget,2\n"
    csv_file.write_bytes(original_content)

    audit_report = audit_csv(csv_file)
    repair_plan = _repair_plan(
        audit_report,
        RepairAction.REMOVE_EXACT_DUPLICATES,
    )
    dataset_policy = DatasetPolicy(
        policy_id="copy-only-policy",
        allowed_actions={
            RepairAction.REMOVE_EXACT_DUPLICATES,
        },
    )

    validate_repair_plan(
        audit_report,
        repair_plan,
        dataset_policy,
    )

    assert csv_file.read_bytes() == original_content
