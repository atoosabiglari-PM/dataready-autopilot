"""Deterministic authorization bridge for Gemini repair proposals."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.policy import (
    DatasetPolicy,
    PolicyDecision,
    RepairPlan,
    validate_repair_plan,
)
from app.services.planner import propose_repair_plan
from app.tools.audit import AuditReport


@dataclass(frozen=True)
class AuthorizedRepairProposal:
    """Gemini proposal paired with its independent policy decision."""

    repair_plan: RepairPlan
    policy_decision: PolicyDecision


def authorize_repair_plan(
    audit_report: AuditReport,
    repair_plan: RepairPlan,
    dataset_policy: DatasetPolicy | None = None,
) -> AuthorizedRepairProposal:
    """Run deterministic policy authorization over an existing repair plan."""

    policy_decision = validate_repair_plan(
        audit_report=audit_report,
        repair_plan=repair_plan,
        dataset_policy=dataset_policy,
    )

    return AuthorizedRepairProposal(
        repair_plan=repair_plan,
        policy_decision=policy_decision,
    )


async def propose_and_authorize_repair(
    audit_report: AuditReport,
    dataset_policy: DatasetPolicy | None = None,
) -> AuthorizedRepairProposal:
    """Ask Gemini for a plan, then independently authorize it."""

    repair_plan = await propose_repair_plan(audit_report)

    return authorize_repair_plan(
        audit_report=audit_report,
        repair_plan=repair_plan,
        dataset_policy=dataset_policy,
    )
