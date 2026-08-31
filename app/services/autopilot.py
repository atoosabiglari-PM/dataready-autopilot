"""End-to-end governed orchestration for DataReady Autopilot."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.core.policy import (
    DatasetPolicy,
    PolicyDecision,
    RepairPlan,
)
from app.services.authorization import propose_and_authorize_repair
from app.services.comparison import (
    ReadinessComparison,
    build_readiness_comparison,
)
from app.services.executor import (
    RepairExecutionError,
    execute_repair_plan,
    prepare_execution_context,
)
from app.services.lineage import (
    DatasetLineageEvidence,
    build_lineage_evidence,
)
from app.services.reporting import (
    MachineReadableReport,
    build_machine_readable_report,
    write_machine_readable_report,
)
from app.tools.audit import AuditReport, audit_csv

AutopilotStatus = Literal[
    "BLOCKED",
    "READY",
    "REQUIRES_REVIEW",
    "DENIED",
    "REPAIRED",
]


@dataclass(frozen=True)
class AutopilotResult:
    """Result of one governed DataReady Autopilot run."""

    status: AutopilotStatus
    audit_report: AuditReport
    repair_plan: RepairPlan | None
    policy_decision: PolicyDecision | None
    output_path: Path | None
    post_repair_audit: AuditReport | None
    readiness_comparison: ReadinessComparison | None
    lineage_evidence: DatasetLineageEvidence | None
    machine_readable_report: MachineReadableReport | None
    report_path: Path | None
    message: str


async def run_autopilot(
    source_file: str | Path,
    output_file: str | Path,
    *,
    dataset_policy: DatasetPolicy | None = None,
) -> AutopilotResult:
    """Run the governed DataReady Autopilot workflow."""

    source_path = Path(source_file)
    output_path = Path(output_file)

    audit_report = audit_csv(source_path)

    if audit_report.status == "BLOCKED":
        return AutopilotResult(
            status="BLOCKED",
            audit_report=audit_report,
            repair_plan=None,
            policy_decision=None,
            output_path=None,
            post_repair_audit=None,
            readiness_comparison=None,
            lineage_evidence=None,
            machine_readable_report=None,
            report_path=None,
            message=(
                "The source dataset failed deterministic safety checks and was not sent to Gemini."
            ),
        )

    if audit_report.status == "READY":
        return AutopilotResult(
            status="READY",
            audit_report=audit_report,
            repair_plan=None,
            policy_decision=None,
            output_path=None,
            post_repair_audit=None,
            readiness_comparison=None,
            lineage_evidence=None,
            machine_readable_report=None,
            report_path=None,
            message=("The deterministic audit found no issues requiring repair."),
        )

    authorized_proposal = await propose_and_authorize_repair(
        audit_report,
        dataset_policy,
    )

    repair_plan = authorized_proposal.repair_plan
    policy_decision = authorized_proposal.policy_decision

    if policy_decision.status == "DENIED":
        return AutopilotResult(
            status="DENIED",
            audit_report=audit_report,
            repair_plan=repair_plan,
            policy_decision=policy_decision,
            output_path=None,
            post_repair_audit=None,
            readiness_comparison=None,
            lineage_evidence=None,
            machine_readable_report=None,
            report_path=None,
            message=(
                "The repair proposal was denied by deterministic policy and was not executed."
            ),
        )

    if policy_decision.status == "REQUIRES_REVIEW" or not policy_decision.can_execute:
        return AutopilotResult(
            status="REQUIRES_REVIEW",
            audit_report=audit_report,
            repair_plan=repair_plan,
            policy_decision=policy_decision,
            output_path=None,
            post_repair_audit=None,
            readiness_comparison=None,
            lineage_evidence=None,
            machine_readable_report=None,
            report_path=None,
            message=("The Gemini repair proposal requires human review before execution."),
        )

    try:
        execution_context = prepare_execution_context(
            source_path,
            output_path,
            repair_plan,
            policy_decision,
        )

        repaired_output = execute_repair_plan(
            execution_context,
        )
    except RepairExecutionError:
        raise

    post_repair_audit = audit_csv(repaired_output)

    readiness_comparison = build_readiness_comparison(
        audit_report,
        post_repair_audit,
    )

    lineage_evidence = build_lineage_evidence(
        source_path,
        repaired_output,
        audit_report,
        post_repair_audit,
        repair_plan,
        policy_decision,
    )

    message = (
        "The approved repair plan was executed on a separate output copy, "
        "independently re-audited, compared with the original audit, "
        "cryptographically linked to the source dataset, and documented "
        "in a machine-readable evidence report."
    )

    machine_readable_report = build_machine_readable_report(
        status="REPAIRED",
        message=message,
        audit_before=audit_report,
        repair_plan=repair_plan,
        policy_decision=policy_decision,
        repaired_csv_path=repaired_output,
        audit_after=post_repair_audit,
        readiness_comparison=readiness_comparison,
        lineage_evidence=lineage_evidence,
    )

    report_path = output_path.with_name(f"{output_path.stem}-report.json")

    written_report_path = write_machine_readable_report(
        machine_readable_report,
        report_path,
    )

    return AutopilotResult(
        status="REPAIRED",
        audit_report=audit_report,
        repair_plan=repair_plan,
        policy_decision=policy_decision,
        output_path=repaired_output,
        post_repair_audit=post_repair_audit,
        readiness_comparison=readiness_comparison,
        lineage_evidence=lineage_evidence,
        machine_readable_report=machine_readable_report,
        report_path=written_report_path,
        message=message,
    )
