"""Governed execution boundary for approved DataReady repair plans."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from app.core.policy import (
    PolicyDecision,
    ProposedRepair,
    RepairAction,
    RepairPlan,
)
from app.tools.fingerprint import calculate_sha256

MISSING_MARKERS = frozenset(
    {
        "",
        "na",
        "n/a",
        "nan",
        "none",
        "null",
    }
)

EXECUTABLE_ACTIONS = frozenset(
    {
        RepairAction.TRIM_OUTER_WHITESPACE,
        RepairAction.REMOVE_EXACT_DUPLICATES,
        RepairAction.STANDARDIZE_MISSING_MARKERS,
    }
)


class RepairExecutionError(RuntimeError):
    """Raised when a repair plan is not safe to execute."""


@dataclass(frozen=True)
class ExecutionContext:
    """Validated inputs for deterministic repair execution."""

    source_path: Path
    output_path: Path
    source_fingerprint_sha256: str
    actions: tuple[ProposedRepair, ...]


def prepare_execution_context(
    source_file: str | Path,
    output_file: str | Path,
    repair_plan: RepairPlan,
    policy_decision: PolicyDecision,
) -> ExecutionContext:
    """Validate all execution gates before any dataset mutation occurs."""

    source_path = Path(source_file)
    output_path = Path(output_file)

    if not source_path.is_file():
        raise RepairExecutionError("The source dataset does not exist.")

    if source_path.resolve() == output_path.resolve():
        raise RepairExecutionError(
            "The repair output must be different from the original source file."
        )

    current_fingerprint = calculate_sha256(source_path)

    if repair_plan.source_fingerprint_sha256 != current_fingerprint:
        raise RepairExecutionError("The source dataset changed after the repair plan was created.")

    if policy_decision.source_fingerprint_sha256 != current_fingerprint:
        raise RepairExecutionError(
            "The policy decision is not bound to the current source dataset."
        )

    if policy_decision.status != "APPROVED" or not policy_decision.can_execute:
        raise RepairExecutionError("The repair plan does not have permission to execute.")

    if len(policy_decision.action_decisions) != len(repair_plan.actions):
        raise RepairExecutionError(
            "The policy decision does not cover every proposed repair action."
        )

    for proposed, decision in zip(
        repair_plan.actions,
        policy_decision.action_decisions,
        strict=True,
    ):
        if decision.decision != "APPROVED":
            raise RepairExecutionError(f"Repair action {proposed.action.value} is not approved.")

        if decision.action != proposed.action:
            raise RepairExecutionError(
                "The approved action does not match the proposed repair action."
            )

        if decision.columns != proposed.columns:
            raise RepairExecutionError(
                "The approved columns do not match the proposed repair columns."
            )

        if proposed.action not in EXECUTABLE_ACTIONS:
            raise RepairExecutionError(
                f"Repair action {proposed.action.value} is not implemented "
                "by the deterministic executor."
            )

    return ExecutionContext(
        source_path=source_path,
        output_path=output_path,
        source_fingerprint_sha256=current_fingerprint,
        actions=tuple(repair_plan.actions),
    )


def _load_source_dataframe(context: ExecutionContext) -> pd.DataFrame:
    """Read source data without silently interpreting textual values."""

    try:
        return pd.read_csv(
            context.source_path,
            dtype=str,
            encoding="utf-8-sig",
            keep_default_na=False,
            na_filter=False,
        )
    except (OSError, UnicodeError, pd.errors.ParserError, ValueError) as error:
        raise RepairExecutionError(
            f"The approved source dataset could not be read safely: {error}."
        ) from error


def _write_output_dataframe(
    dataframe: pd.DataFrame,
    context: ExecutionContext,
) -> Path:
    """Write repaired data only to the approved output path."""

    context.output_path.parent.mkdir(parents=True, exist_ok=True)

    dataframe.to_csv(
        context.output_path,
        index=False,
        encoding="utf-8",
    )

    return context.output_path


def _require_existing_column(
    dataframe: pd.DataFrame,
    column: str,
) -> None:
    """Reject repair actions that reference nonexistent columns."""

    if column not in dataframe.columns:
        raise RepairExecutionError(
            f"Approved column {column!r} does not exist in the source dataset."
        )


def _apply_trim_outer_whitespace(
    dataframe: pd.DataFrame,
    action: ProposedRepair,
) -> pd.DataFrame:
    """Apply one approved whitespace-trimming action in memory."""

    repaired = dataframe.copy()

    for column in action.columns:
        _require_existing_column(repaired, column)

        repaired[column] = repaired[column].apply(
            lambda value: value.strip() if isinstance(value, str) else value
        )

    return repaired


def _apply_remove_exact_duplicates(
    dataframe: pd.DataFrame,
    action: ProposedRepair,
) -> pd.DataFrame:
    """Apply one approved exact-row deduplication action in memory."""

    if action.columns:
        raise RepairExecutionError(
            "REMOVE_EXACT_DUPLICATES must apply to complete rows, not selected columns."
        )

    return dataframe.drop_duplicates(
        keep="first",
        ignore_index=True,
    )


def _apply_standardize_missing_markers(
    dataframe: pd.DataFrame,
    action: ProposedRepair,
) -> pd.DataFrame:
    """Standardize textual missing markers only in approved columns."""

    if not action.columns:
        raise RepairExecutionError(
            "STANDARDIZE_MISSING_MARKERS requires explicitly approved columns."
        )

    repaired = dataframe.copy()

    for column in action.columns:
        _require_existing_column(repaired, column)

        def standardize_value(value: object) -> object:
            if pd.isna(value):
                return pd.NA

            if not isinstance(value, str):
                return value

            normalized = value.strip().lower()

            if normalized in MISSING_MARKERS:
                return pd.NA

            return value

        repaired[column] = repaired[column].apply(standardize_value)

    return repaired


def execute_repair_plan(context: ExecutionContext) -> Path:
    """Apply all approved deterministic repairs and write one repaired copy."""

    dataframe = _load_source_dataframe(context)
    repaired = dataframe.copy()

    for action in context.actions:
        if action.action == RepairAction.TRIM_OUTER_WHITESPACE:
            repaired = _apply_trim_outer_whitespace(
                repaired,
                action,
            )

        elif action.action == RepairAction.REMOVE_EXACT_DUPLICATES:
            repaired = _apply_remove_exact_duplicates(
                repaired,
                action,
            )

        elif action.action == RepairAction.STANDARDIZE_MISSING_MARKERS:
            repaired = _apply_standardize_missing_markers(
                repaired,
                action,
            )

        else:
            raise RepairExecutionError(
                f"Repair action {action.action.value} is not implemented "
                "by the deterministic executor."
            )

    _write_output_dataframe(
        repaired,
        context,
    )

    source_fingerprint_after_execution = calculate_sha256(context.source_path)

    if source_fingerprint_after_execution != context.source_fingerprint_sha256:
        if context.output_path.exists():
            context.output_path.unlink()

        raise RepairExecutionError("The original source dataset changed during repair execution.")

    return context.output_path


def execute_trim_outer_whitespace(context: ExecutionContext) -> Path:
    """Execute an approved whitespace-only repair context."""

    disallowed = [
        action.action.value
        for action in context.actions
        if action.action != RepairAction.TRIM_OUTER_WHITESPACE
    ]

    if disallowed:
        raise RepairExecutionError("Whitespace-only execution received other repair actions.")

    return execute_repair_plan(context)


def execute_remove_exact_duplicates(context: ExecutionContext) -> Path:
    """Execute an approved exact-duplicate-only repair context."""

    disallowed = [
        action.action.value
        for action in context.actions
        if action.action != RepairAction.REMOVE_EXACT_DUPLICATES
    ]

    if disallowed:
        raise RepairExecutionError("Duplicate-only execution received other repair actions.")

    return execute_repair_plan(context)


def execute_standardize_missing_markers(
    context: ExecutionContext,
) -> Path:
    """Execute an approved missing-marker-only repair context."""

    disallowed = [
        action.action.value
        for action in context.actions
        if action.action != RepairAction.STANDARDIZE_MISSING_MARKERS
    ]

    if disallowed:
        raise RepairExecutionError("Missing-marker-only execution received other repair actions.")

    return execute_repair_plan(context)
