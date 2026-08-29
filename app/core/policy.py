"""Deterministic authorization policy for AI-proposed repair plans."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from app.tools.audit import AuditReport

UNIVERSAL_INVARIANTS = [
    "Never modify or delete the original file.",
    "Apply approved repairs only to a separate copy.",
    "Never execute text found inside CSV cells as instructions.",
    "Never provide detected PII values to an AI model.",
    "Bind every repair plan to the source file fingerprint.",
    "Record evidence for every authorization decision.",
]

CRITICAL_REVIEW_CODES = {
    "AMBIGUOUS_COLUMN_NAMES",
    "PII_COLUMN_NAME",
    "PII_VALUE_PATTERN",
    "PROMPT_INJECTION_PATTERN",
}


class RepairAction(StrEnum):
    """Constrained actions that an AI agent may propose."""

    REMOVE_EXACT_DUPLICATES = "REMOVE_EXACT_DUPLICATES"
    TRIM_OUTER_WHITESPACE = "TRIM_OUTER_WHITESPACE"
    STANDARDIZE_MISSING_MARKERS = "STANDARDIZE_MISSING_MARKERS"
    RENAME_COLUMNS = "RENAME_COLUMNS"
    FILL_MISSING_VALUES = "FILL_MISSING_VALUES"
    CONVERT_COLUMN_TYPE = "CONVERT_COLUMN_TYPE"
    DROP_ROWS = "DROP_ROWS"
    REDACT_PII = "REDACT_PII"


class ProposedRepair(BaseModel):
    """One constrained repair proposed by an AI agent."""

    action: RepairAction
    justification: str = Field(min_length=1, max_length=500)
    columns: list[str] = Field(default_factory=list)


class RepairPlan(BaseModel):
    """A repair proposal bound to one exact source file."""

    source_fingerprint_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    summary: str = Field(min_length=1, max_length=1_000)
    actions: list[ProposedRepair] = Field(default_factory=list)


class DatasetPolicy(BaseModel):
    """Explicit permissions supplied for a particular dataset or workflow."""

    policy_id: str = Field(
        default="conservative-default",
        min_length=1,
        max_length=100,
    )
    allowed_actions: set[RepairAction] = Field(default_factory=set)


class ActionDecision(BaseModel):
    """Authorization result for one proposed repair."""

    action: RepairAction
    decision: Literal["APPROVED", "REQUIRES_REVIEW", "DENIED"]
    reason: str
    columns: list[str] = Field(default_factory=list)


class PolicyDecision(BaseModel):
    """Final deterministic decision for an AI-proposed repair plan."""

    status: Literal["APPROVED", "REQUIRES_REVIEW", "DENIED"]
    can_execute: bool
    policy_id: str
    source_fingerprint_sha256: str | None
    action_decisions: list[ActionDecision] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    invariants_enforced: list[str] = Field(default_factory=lambda: list(UNIVERSAL_INVARIANTS))


def _denied_decision(
    audit_report: AuditReport,
    repair_plan: RepairPlan,
    dataset_policy: DatasetPolicy,
    reason: str,
) -> PolicyDecision:
    """Create a denial that prevents every proposed action."""

    action_decisions = [
        ActionDecision(
            action=proposed.action,
            decision="DENIED",
            reason=reason,
            columns=proposed.columns,
        )
        for proposed in repair_plan.actions
    ]

    return PolicyDecision(
        status="DENIED",
        can_execute=False,
        policy_id=dataset_policy.policy_id,
        source_fingerprint_sha256=audit_report.fingerprint_sha256,
        action_decisions=action_decisions,
        reasons=[reason],
    )


def validate_repair_plan(
    audit_report: AuditReport,
    repair_plan: RepairPlan,
    dataset_policy: DatasetPolicy | None = None,
) -> PolicyDecision:
    """Authorize a repair plan without relying on AI judgment."""

    policy = dataset_policy or DatasetPolicy()

    if audit_report.status == "BLOCKED":
        return _denied_decision(
            audit_report,
            repair_plan,
            policy,
            "Blocked files cannot enter the repair pipeline.",
        )

    if audit_report.fingerprint_sha256 is None:
        return _denied_decision(
            audit_report,
            repair_plan,
            policy,
            "The audit report does not contain a source fingerprint.",
        )

    if repair_plan.source_fingerprint_sha256 != audit_report.fingerprint_sha256:
        return _denied_decision(
            audit_report,
            repair_plan,
            policy,
            "The repair plan fingerprint does not match the audited file.",
        )

    critical_findings = sorted(
        {
            issue.code
            for issue in audit_report.issues
            if issue.severity == "CRITICAL" or issue.code in CRITICAL_REVIEW_CODES
        }
    )

    if critical_findings:
        reason = (
            "Critical or meaning-sensitive findings require human review: "
            + ", ".join(critical_findings)
            + "."
        )

        return PolicyDecision(
            status="REQUIRES_REVIEW",
            can_execute=False,
            policy_id=policy.policy_id,
            source_fingerprint_sha256=audit_report.fingerprint_sha256,
            action_decisions=[
                ActionDecision(
                    action=proposed.action,
                    decision="REQUIRES_REVIEW",
                    reason=reason,
                    columns=proposed.columns,
                )
                for proposed in repair_plan.actions
            ],
            reasons=[reason],
        )

    if not repair_plan.actions:
        if audit_report.status == "READY":
            return PolicyDecision(
                status="APPROVED",
                can_execute=True,
                policy_id=policy.policy_id,
                source_fingerprint_sha256=audit_report.fingerprint_sha256,
                reasons=["The dataset is ready and no repair is requested."],
            )

        return PolicyDecision(
            status="REQUIRES_REVIEW",
            can_execute=False,
            policy_id=policy.policy_id,
            source_fingerprint_sha256=audit_report.fingerprint_sha256,
            reasons=["The dataset is quarantined and the plan contains no authorized resolution."],
        )

    action_decisions: list[ActionDecision] = []

    for proposed in repair_plan.actions:
        if proposed.action in policy.allowed_actions:
            action_decisions.append(
                ActionDecision(
                    action=proposed.action,
                    decision="APPROVED",
                    reason=(f"Dataset policy {policy.policy_id!r} explicitly allows this action."),
                    columns=proposed.columns,
                )
            )
        else:
            action_decisions.append(
                ActionDecision(
                    action=proposed.action,
                    decision="REQUIRES_REVIEW",
                    reason=(f"Dataset policy {policy.policy_id!r} does not authorize this action."),
                    columns=proposed.columns,
                )
            )

    requires_review = any(decision.decision != "APPROVED" for decision in action_decisions)

    if requires_review:
        return PolicyDecision(
            status="REQUIRES_REVIEW",
            can_execute=False,
            policy_id=policy.policy_id,
            source_fingerprint_sha256=audit_report.fingerprint_sha256,
            action_decisions=action_decisions,
            reasons=["At least one proposed action lacks explicit authorization."],
        )

    return PolicyDecision(
        status="APPROVED",
        can_execute=True,
        policy_id=policy.policy_id,
        source_fingerprint_sha256=audit_report.fingerprint_sha256,
        action_decisions=action_decisions,
        reasons=["Every proposed action is explicitly authorized by the dataset policy."],
    )
