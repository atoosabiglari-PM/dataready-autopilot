"""Tests for the governed repair execution boundary."""

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
from app.services.executor import (
    ExecutionContext,
    RepairExecutionError,
    execute_remove_exact_duplicates,
    execute_standardize_missing_markers,
    execute_trim_outer_whitespace,
    prepare_execution_context,
)
from app.tools.fingerprint import calculate_sha256


def make_source(tmp_path: Path) -> Path:
    """Create a small deterministic CSV source file."""

    source = tmp_path / "source.csv"
    source.write_text("name\n Alice \n", encoding="utf-8")
    return source


def make_plan(fingerprint: str) -> RepairPlan:
    """Create one approved-style repair proposal."""

    return RepairPlan(
        source_fingerprint_sha256=fingerprint,
        summary="Trim outer whitespace.",
        actions=[
            ProposedRepair(
                action=RepairAction.TRIM_OUTER_WHITESPACE,
                justification="Whitespace was detected.",
                columns=["name"],
            )
        ],
    )


def make_approved_decision(fingerprint: str) -> PolicyDecision:
    """Create a matching deterministic policy approval."""

    return PolicyDecision(
        status="APPROVED",
        can_execute=True,
        policy_id="test-policy",
        source_fingerprint_sha256=fingerprint,
        action_decisions=[
            ActionDecision(
                action=RepairAction.TRIM_OUTER_WHITESPACE,
                decision="APPROVED",
                reason="Explicitly authorized for testing.",
                columns=["name"],
            )
        ],
        reasons=["Every proposed action is explicitly authorized."],
    )


def test_prepares_execution_context_for_fully_approved_plan(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    output = tmp_path / "repaired.csv"
    fingerprint = calculate_sha256(source)

    context = prepare_execution_context(
        source,
        output,
        make_plan(fingerprint),
        make_approved_decision(fingerprint),
    )

    assert context.source_path == source
    assert context.output_path == output
    assert context.source_fingerprint_sha256 == fingerprint
    assert len(context.actions) == 1
    assert context.actions[0].action == RepairAction.TRIM_OUTER_WHITESPACE


def test_rejects_attempt_to_overwrite_original_file(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    fingerprint = calculate_sha256(source)

    with pytest.raises(
        RepairExecutionError,
        match="output must be different",
    ):
        prepare_execution_context(
            source,
            source,
            make_plan(fingerprint),
            make_approved_decision(fingerprint),
        )


def test_rejects_source_changed_after_plan_creation(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    original_fingerprint = calculate_sha256(source)

    plan = make_plan(original_fingerprint)
    decision = make_approved_decision(original_fingerprint)

    source.write_text("name\nBob\n", encoding="utf-8")

    with pytest.raises(
        RepairExecutionError,
        match="changed after the repair plan",
    ):
        prepare_execution_context(
            source,
            tmp_path / "repaired.csv",
            plan,
            decision,
        )


def test_rejects_policy_without_execution_permission(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    fingerprint = calculate_sha256(source)

    decision = make_approved_decision(fingerprint).model_copy(
        update={
            "status": "REQUIRES_REVIEW",
            "can_execute": False,
        }
    )

    with pytest.raises(
        RepairExecutionError,
        match="does not have permission",
    ):
        prepare_execution_context(
            source,
            tmp_path / "repaired.csv",
            make_plan(fingerprint),
            decision,
        )


def test_rejects_policy_that_does_not_cover_every_action(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    fingerprint = calculate_sha256(source)

    decision = make_approved_decision(fingerprint).model_copy(update={"action_decisions": []})

    with pytest.raises(
        RepairExecutionError,
        match="does not cover every proposed repair action",
    ):
        prepare_execution_context(
            source,
            tmp_path / "repaired.csv",
            make_plan(fingerprint),
            decision,
        )


def test_rejects_mismatched_approved_columns(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    fingerprint = calculate_sha256(source)

    decision = make_approved_decision(fingerprint).model_copy(
        update={
            "action_decisions": [
                ActionDecision(
                    action=RepairAction.TRIM_OUTER_WHITESPACE,
                    decision="APPROVED",
                    reason="Approved.",
                    columns=["different_column"],
                )
            ]
        }
    )

    with pytest.raises(
        RepairExecutionError,
        match="approved columns do not match",
    ):
        prepare_execution_context(
            source,
            tmp_path / "repaired.csv",
            make_plan(fingerprint),
            decision,
        )


def test_trim_outer_whitespace_creates_repaired_copy(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text(
        "name,city\n Alice , Palo Alto \n Bob ,San Jose\n",
        encoding="utf-8",
    )

    original_contents = source.read_text(encoding="utf-8")
    fingerprint = calculate_sha256(source)
    output = tmp_path / "repaired.csv"

    plan = RepairPlan(
        source_fingerprint_sha256=fingerprint,
        summary="Trim name whitespace.",
        actions=[
            ProposedRepair(
                action=RepairAction.TRIM_OUTER_WHITESPACE,
                justification="Whitespace was detected.",
                columns=["name"],
            )
        ],
    )

    decision = PolicyDecision(
        status="APPROVED",
        can_execute=True,
        policy_id="test-policy",
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
    )

    context = prepare_execution_context(
        source,
        output,
        plan,
        decision,
    )

    result = execute_trim_outer_whitespace(context)

    assert result == output
    assert output.is_file()
    assert source.read_text(encoding="utf-8") == original_contents

    repaired = pd.read_csv(output, dtype=str, keep_default_na=True)

    assert repaired["name"].tolist() == ["Alice", "Bob"]
    assert repaired["city"].tolist() == [" Palo Alto ", "San Jose"]


def test_trim_rejects_nonexistent_approved_column(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    fingerprint = calculate_sha256(source)

    action = ProposedRepair(
        action=RepairAction.TRIM_OUTER_WHITESPACE,
        justification="Test missing column.",
        columns=["does_not_exist"],
    )

    context = ExecutionContext(
        source_path=source,
        output_path=tmp_path / "repaired.csv",
        source_fingerprint_sha256=fingerprint,
        actions=(action,),
    )

    with pytest.raises(
        RepairExecutionError,
        match="does not exist",
    ):
        execute_trim_outer_whitespace(context)

    assert not context.output_path.exists()


def test_remove_exact_duplicates_creates_repaired_copy(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text(
        "name,city\nAlice,Palo Alto\nAlice,Palo Alto\nBob,San Jose\n",
        encoding="utf-8",
    )

    original_contents = source.read_text(encoding="utf-8")
    fingerprint = calculate_sha256(source)
    output = tmp_path / "repaired.csv"

    plan = RepairPlan(
        source_fingerprint_sha256=fingerprint,
        summary="Remove exact duplicate rows.",
        actions=[
            ProposedRepair(
                action=RepairAction.REMOVE_EXACT_DUPLICATES,
                justification="Exact duplicate rows were detected.",
                columns=[],
            )
        ],
    )

    decision = PolicyDecision(
        status="APPROVED",
        can_execute=True,
        policy_id="test-policy",
        source_fingerprint_sha256=fingerprint,
        action_decisions=[
            ActionDecision(
                action=RepairAction.REMOVE_EXACT_DUPLICATES,
                decision="APPROVED",
                reason="Explicitly authorized.",
                columns=[],
            )
        ],
        reasons=["Approved."],
    )

    context = prepare_execution_context(
        source,
        output,
        plan,
        decision,
    )

    result = execute_remove_exact_duplicates(context)

    assert result == output
    assert output.is_file()
    assert source.read_text(encoding="utf-8") == original_contents

    repaired = pd.read_csv(output, dtype=str, keep_default_na=True)

    assert len(repaired.index) == 2
    assert repaired["name"].tolist() == ["Alice", "Bob"]
    assert repaired["city"].tolist() == ["Palo Alto", "San Jose"]


def test_duplicate_removal_rejects_column_scoped_action(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text(
        "name,city\nAlice,Palo Alto\nAlice,San Jose\n",
        encoding="utf-8",
    )

    fingerprint = calculate_sha256(source)

    action = ProposedRepair(
        action=RepairAction.REMOVE_EXACT_DUPLICATES,
        justification="Invalid column-scoped duplicate removal.",
        columns=["name"],
    )

    context = ExecutionContext(
        source_path=source,
        output_path=tmp_path / "repaired.csv",
        source_fingerprint_sha256=fingerprint,
        actions=(action,),
    )

    with pytest.raises(
        RepairExecutionError,
        match="must apply to complete rows",
    ):
        execute_remove_exact_duplicates(context)

    assert not context.output_path.exists()


def test_standardize_missing_markers_only_in_approved_columns(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.csv"
    source.write_text(
        "name,status\nAlice,N/A\nN/A,active\nBob, null \nCarol,unknown\n",
        encoding="utf-8",
    )

    original_contents = source.read_text(encoding="utf-8")
    fingerprint = calculate_sha256(source)
    output = tmp_path / "repaired.csv"

    plan = RepairPlan(
        source_fingerprint_sha256=fingerprint,
        summary="Standardize missing markers in status.",
        actions=[
            ProposedRepair(
                action=RepairAction.STANDARDIZE_MISSING_MARKERS,
                justification="Textual missing markers were detected.",
                columns=["status"],
            )
        ],
    )

    decision = PolicyDecision(
        status="APPROVED",
        can_execute=True,
        policy_id="test-policy",
        source_fingerprint_sha256=fingerprint,
        action_decisions=[
            ActionDecision(
                action=RepairAction.STANDARDIZE_MISSING_MARKERS,
                decision="APPROVED",
                reason="Explicitly authorized.",
                columns=["status"],
            )
        ],
        reasons=["Approved."],
    )

    context = prepare_execution_context(
        source,
        output,
        plan,
        decision,
    )

    result = execute_standardize_missing_markers(context)

    assert result == output
    assert output.is_file()
    assert source.read_text(encoding="utf-8") == original_contents

    repaired = pd.read_csv(
        output,
        dtype=str,
        keep_default_na=False,
    )

    # Only the approved "status" column is standardized.
    assert repaired["status"].tolist() == ["", "active", "", "unknown"]

    # "N/A" in the unapproved name column must remain unchanged.
    assert repaired["name"].tolist() == ["Alice", "N/A", "Bob", "Carol"]


def test_missing_marker_standardization_requires_columns(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    fingerprint = calculate_sha256(source)

    action = ProposedRepair(
        action=RepairAction.STANDARDIZE_MISSING_MARKERS,
        justification="Invalid unscoped missing-marker repair.",
        columns=[],
    )

    context = ExecutionContext(
        source_path=source,
        output_path=tmp_path / "repaired.csv",
        source_fingerprint_sha256=fingerprint,
        actions=(action,),
    )

    with pytest.raises(
        RepairExecutionError,
        match="requires explicitly approved columns",
    ):
        execute_standardize_missing_markers(context)

    assert not context.output_path.exists()


def test_execute_repair_plan_applies_multiple_actions_to_copy(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.csv"
    source.write_text(
        "name,status\n Alice ,N/A\n Alice ,N/A\n Bob ,active\n",
        encoding="utf-8",
    )

    original_contents = source.read_bytes()
    fingerprint = calculate_sha256(source)
    output = tmp_path / "repaired.csv"

    plan = RepairPlan(
        source_fingerprint_sha256=fingerprint,
        summary="Apply approved deterministic repairs.",
        actions=[
            ProposedRepair(
                action=RepairAction.TRIM_OUTER_WHITESPACE,
                justification="Outer whitespace was detected.",
                columns=["name"],
            ),
            ProposedRepair(
                action=RepairAction.STANDARDIZE_MISSING_MARKERS,
                justification="Missing markers were detected.",
                columns=["status"],
            ),
            ProposedRepair(
                action=RepairAction.REMOVE_EXACT_DUPLICATES,
                justification="Exact duplicate rows were detected.",
                columns=[],
            ),
        ],
    )

    decision = PolicyDecision(
        status="APPROVED",
        can_execute=True,
        policy_id="test-policy",
        source_fingerprint_sha256=fingerprint,
        action_decisions=[
            ActionDecision(
                action=RepairAction.TRIM_OUTER_WHITESPACE,
                decision="APPROVED",
                reason="Authorized.",
                columns=["name"],
            ),
            ActionDecision(
                action=RepairAction.STANDARDIZE_MISSING_MARKERS,
                decision="APPROVED",
                reason="Authorized.",
                columns=["status"],
            ),
            ActionDecision(
                action=RepairAction.REMOVE_EXACT_DUPLICATES,
                decision="APPROVED",
                reason="Authorized.",
                columns=[],
            ),
        ],
        reasons=["All actions approved."],
    )

    context = prepare_execution_context(
        source,
        output,
        plan,
        decision,
    )

    from app.services.executor import execute_repair_plan

    result = execute_repair_plan(context)

    assert result == output
    assert output.is_file()

    # The original file must remain byte-for-byte unchanged.
    assert source.read_bytes() == original_contents
    assert calculate_sha256(source) == fingerprint

    repaired = pd.read_csv(
        output,
        dtype=str,
        keep_default_na=False,
    )

    assert repaired["name"].tolist() == ["Alice", "Bob"]
    assert repaired["status"].tolist() == ["", "active"]
    assert len(repaired.index) == 2


def test_execution_deletes_output_if_source_integrity_check_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.csv"
    source.write_text(
        "name\n Alice \n",
        encoding="utf-8",
    )

    fingerprint = calculate_sha256(source)
    output = tmp_path / "repaired.csv"

    plan = RepairPlan(
        source_fingerprint_sha256=fingerprint,
        summary="Trim whitespace.",
        actions=[
            ProposedRepair(
                action=RepairAction.TRIM_OUTER_WHITESPACE,
                justification="Whitespace was detected.",
                columns=["name"],
            )
        ],
    )

    decision = PolicyDecision(
        status="APPROVED",
        can_execute=True,
        policy_id="test-policy",
        source_fingerprint_sha256=fingerprint,
        action_decisions=[
            ActionDecision(
                action=RepairAction.TRIM_OUTER_WHITESPACE,
                decision="APPROVED",
                reason="Authorized.",
                columns=["name"],
            )
        ],
        reasons=["Approved."],
    )

    context = prepare_execution_context(
        source,
        output,
        plan,
        decision,
    )

    monkeypatch.setattr(
        "app.services.executor.calculate_sha256",
        lambda path: "b" * 64,
    )

    from app.services.executor import execute_repair_plan

    with pytest.raises(
        RepairExecutionError,
        match="changed during repair execution",
    ):
        execute_repair_plan(context)

    # Never leave behind an output whose source integrity check failed.
    assert not output.exists()
