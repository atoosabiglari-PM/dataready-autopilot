"""Command-line demo interface for DataReady Autopilot."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path

from app.core.policy import DatasetPolicy, RepairAction
from app.services.autopilot import AutopilotResult, run_autopilot

SAFE_EXECUTABLE_ACTIONS = {
    RepairAction.TRIM_OUTER_WHITESPACE,
    RepairAction.REMOVE_EXACT_DUPLICATES,
    RepairAction.STANDARDIZE_MISSING_MARKERS,
}


def build_parser() -> argparse.ArgumentParser:
    """Build the DataReady Autopilot command-line parser."""

    parser = argparse.ArgumentParser(
        prog="dataready-autopilot",
        description=(
            "Audit a CSV, obtain a constrained Gemini repair proposal, "
            "apply deterministic policy controls, repair only an output "
            "copy, re-audit it, and generate evidence."
        ),
    )

    parser.add_argument(
        "source",
        type=Path,
        help="Path to the source CSV file.",
    )

    parser.add_argument(
        "output",
        type=Path,
        help="Path for the repaired CSV output.",
    )

    parser.add_argument(
        "--allow-safe-repairs",
        action="store_true",
        help=(
            "Explicitly allow the three implemented deterministic "
            "low-risk repair actions. Without this flag, proposed "
            "repairs require policy review."
        ),
    )

    return parser


def build_dataset_policy(
    *,
    allow_safe_repairs: bool,
) -> DatasetPolicy | None:
    """Build the explicit demo policy selected by the operator."""

    if not allow_safe_repairs:
        return None

    return DatasetPolicy(
        policy_id="demo-safe-auto-repairs",
        allowed_actions=set(SAFE_EXECUTABLE_ACTIONS),
    )


def print_result(
    result: AutopilotResult,
) -> None:
    """Print a concise competition-demo summary."""

    print()
    print("=" * 68)
    print("DataReady Autopilot")
    print("=" * 68)

    print(f"Status: {result.status}")
    print(f"Source: {result.audit_report.file_name}")
    print(f"Source SHA-256: {result.audit_report.fingerprint_sha256 or 'Unavailable'}")
    print(f"Initial readiness: {result.audit_report.status}")
    print(f"Initial quality score: {result.audit_report.quality_score}")

    if result.repair_plan is not None:
        print()
        print("Gemini repair proposal:")

        if result.repair_plan.actions:
            for action in result.repair_plan.actions:
                columns = ", ".join(action.columns) if action.columns else "dataset-level"

                print(f"  - {action.action.value} [{columns}]")
        else:
            print("  - No repair actions proposed.")

    if result.policy_decision is not None:
        print()
        print(f"Deterministic policy decision: {result.policy_decision.status}")
        print(f"Execution authorized: {result.policy_decision.can_execute}")

    if result.output_path is not None:
        print()
        print(f"Repaired CSV: {result.output_path}")

    if result.post_repair_audit is not None:
        print(f"Post-repair readiness: {result.post_repair_audit.status}")
        print(f"Post-repair quality score: {result.post_repair_audit.quality_score}")

    if result.readiness_comparison is not None:
        comparison = result.readiness_comparison

        print()
        print("Before / after evidence:")
        print(
            "  Quality score: "
            f"{comparison.before_quality_score}"
            " -> "
            f"{comparison.after_quality_score}"
            f" ({comparison.quality_score_delta:+d})"
        )
        print(f"  Findings: {comparison.before_issue_count} -> {comparison.after_issue_count}")

        if comparison.resolved_issue_codes:
            print("  Resolved: " + ", ".join(comparison.resolved_issue_codes))

        if comparison.remaining_issue_codes:
            print("  Remaining: " + ", ".join(comparison.remaining_issue_codes))

        if comparison.new_issue_codes:
            print("  New findings: " + ", ".join(comparison.new_issue_codes))

    if result.lineage_evidence is not None:
        lineage = result.lineage_evidence

        print()
        print("Cryptographic lineage:")
        print(f"  Source preserved: {lineage.source_preserved}")
        print(f"  Source SHA-256: {lineage.source_fingerprint_sha256}")
        print(f"  Output SHA-256: {lineage.output_fingerprint_sha256}")

    if result.report_path is not None:
        print()
        print(f"Evidence report: {result.report_path}")

    print()
    print(result.message)
    print("=" * 68)
    print()


def exit_code_for_result(
    result: AutopilotResult,
) -> int:
    """Map governed Autopilot outcomes to CLI exit codes."""

    if result.status in {
        "READY",
        "REPAIRED",
    }:
        return 0

    if result.status == "REQUIRES_REVIEW":
        return 2

    if result.status == "DENIED":
        return 3

    if result.status == "BLOCKED":
        return 4

    return 1


async def run_cli(
    args: argparse.Namespace,
) -> int:
    """Execute one CLI-requested Autopilot run."""

    dataset_policy = build_dataset_policy(
        allow_safe_repairs=args.allow_safe_repairs,
    )

    result = await run_autopilot(
        args.source,
        args.output,
        dataset_policy=dataset_policy,
    )

    print_result(result)

    return exit_code_for_result(result)


def main(
    argv: Sequence[str] | None = None,
) -> int:
    """Run the DataReady Autopilot command-line interface."""

    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return asyncio.run(run_cli(args))
    except KeyboardInterrupt:
        print()
        print("DataReady Autopilot cancelled.")
        return 130
    except Exception as exc:
        print()
        print("=" * 68)
        print("DataReady Autopilot failed safely.")
        print("=" * 68)
        print(f"{type(exc).__name__}: {exc}")
        print()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
