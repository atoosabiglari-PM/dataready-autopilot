"""Regression-check unit tests using synthetic audit evidence, not model calls."""

import pytest
from pydantic import ValidationError

from app.core.policy import ProposedRepair, RepairAction, RepairPlan
from app.services.regression import RegressionInputError, assess_audit_regressions
from app.tools.audit import AuditIssue, AuditReport
from app.tools.preflight import PreflightReport

SOURCE = "a" * 64
OUTPUT = "b" * 64


def issue(code="MISSING_VALUES", column="item", count=1, severity="WARNING"):
    return AuditIssue(code=code, column=column, count=count, severity=severity, message="Test.")


def report(fingerprint=SOURCE, *, issues=(), rows=10, columns=2,
           duplicates=0, score=90, status="QUARANTINED"):
    return AuditReport(
        status=status, file_name="fixture.csv", fingerprint_sha256=fingerprint,
        row_count=rows, column_count=columns, duplicate_row_count=duplicates,
        quality_score=score, issues=list(issues),
        preflight=PreflightReport(
            status="BLOCKED" if status == "BLOCKED" else "ACCEPTED",
            file_name="fixture.csv", file_size_bytes=100,
        ),
    )


def plan(action=None):
    actions = [] if action is None else [ProposedRepair(
        action=action, justification="Test.",
        columns=[] if action == RepairAction.REMOVE_EXACT_DUPLICATES else ["item"],
    )]
    return RepairPlan(source_fingerprint_sha256=SOURCE, summary="Test.", actions=actions)


def assess(before, after, repair_plan=None):
    return assess_audit_regressions(
        before, after, repair_plan or plan(), expected_output_fingerprint_sha256=OUTPUT
    )


def codes(result):
    return {finding.code for finding in result.findings}


def test_unchanged_findings_are_not_called_an_improvement():
    result = assess(report(issues=[issue()]), report(OUTPUT, issues=[issue()]))
    assert result.status == "NO_AUDIT_REGRESSION"
    assert not hasattr(result, "verified")
    assert not hasattr(result, "can_execute")
    assert not hasattr(result, "release_authorized")


def test_new_finding_is_detected_even_when_quality_score_improves():
    result = assess(report(score=60), report(OUTPUT, issues=[issue()], score=90))
    assert "NEW_FINDING" in codes(result)
    assert result.status == "REGRESSION_DETECTED"


def test_same_code_on_a_different_column_is_new():
    result = assess(
        report(issues=[issue(column="a")]),
        report(OUTPUT, issues=[issue(column="b")]),
    )
    assert any(f.code == "NEW_FINDING" and f.column == "b" for f in result.findings)


def test_worsening_count_is_detected_with_unchanged_quality_score():
    result = assess(
        report(issues=[issue(count=2)]),
        report(OUTPUT, issues=[issue(count=3)]),
    )
    assert "FINDING_COUNT_INCREASED" in codes(result)


@pytest.mark.parametrize("old,new", [
    ("INFO", "WARNING"),
    ("WARNING", "CRITICAL"),
    ("INFO", "CRITICAL"),
])
def test_severity_escalation_is_detected(old, new):
    result = assess(
        report(issues=[issue(severity=old)]),
        report(OUTPUT, issues=[issue(severity=new)]),
    )
    assert "SEVERITY_INCREASED" in codes(result)


def test_decreasing_count_is_not_a_regression():
    result = assess(
        report(issues=[issue(count=3)]),
        report(OUTPUT, issues=[issue(count=1)]),
    )
    assert result.status == "NO_AUDIT_REGRESSION"


def test_repeated_findings_are_aggregated_not_overwritten():
    result = assess(
        report(issues=[issue(count=2)]),
        report(OUTPUT, issues=[issue(count=2), issue(count=1)]),
    )
    assert "FINDING_COUNT_INCREASED" in codes(result)


def test_grouped_findings_retain_highest_severity():
    result = assess(
        report(issues=[issue(severity="CRITICAL"), issue()]),
        report(OUTPUT, issues=[issue(severity="CRITICAL", count=2)]),
    )
    assert "SEVERITY_INCREASED" not in codes(result)


@pytest.mark.parametrize("columns", [1, 3])
def test_column_count_change_is_detected(columns):
    result = assess(report(), report(OUTPUT, columns=columns))
    assert "COLUMN_COUNT_CHANGED" in codes(result)


def test_added_rows_are_detected():
    assert "ROWS_ADDED" in codes(assess(report(), report(OUTPUT, rows=11)))


def test_row_removal_without_dedupe_is_detected():
    result = assess(
        report(duplicates=1),
        report(OUTPUT, rows=9),
        plan(RepairAction.TRIM_OUTER_WHITESPACE),
    )
    assert "UNEXPECTED_ROW_REMOVAL" in codes(result)


def test_permitted_deduplication_within_original_duplicate_budget():
    result = assess(
        report(duplicates=1),
        report(OUTPUT, rows=9),
        plan(RepairAction.REMOVE_EXACT_DUPLICATES),
    )
    assert result.status == "NO_AUDIT_REGRESSION"


def test_excessive_row_removal_is_detected():
    result = assess(
        report(duplicates=1),
        report(OUTPUT, rows=8),
        plan(RepairAction.REMOVE_EXACT_DUPLICATES),
    )
    assert "EXCESS_ROW_REMOVAL" in codes(result)


def test_increased_duplicate_count_is_detected():
    result = assess(report(), report(OUTPUT, duplicates=1))
    assert "DUPLICATES_INCREASED" in codes(result)


def test_ready_to_quarantined_is_detected():
    result = assess(
        report(status="READY", score=100),
        report(OUTPUT, issues=[issue()], score=90),
    )
    assert "READINESS_REGRESSION" in codes(result)
    assert "QUALITY_SCORE_DECREASED" in codes(result)


def test_quality_score_drop_alone_is_detected():
    result = assess(report(), report(OUTPUT, score=80))
    assert "QUALITY_SCORE_DECREASED" in codes(result)


def test_blocked_output_cannot_pass():
    result = assess(report(), report(OUTPUT, status="BLOCKED", score=0, rows=0))
    assert codes(result) == {"OUTPUT_BLOCKED"}


def test_blocked_source_cannot_be_evaluated():
    with pytest.raises(RegressionInputError, match="blocked source"):
        assess(report(status="BLOCKED"), report(OUTPUT))


def test_wrong_source_fingerprint_rejected():
    with pytest.raises(RegressionInputError, match="source audit"):
        assess(report("c" * 64), report(OUTPUT))


@pytest.mark.parametrize("fingerprint", [None, "invalid", "c" * 64])
def test_mismatched_output_evidence_rejected(fingerprint):
    with pytest.raises(RegressionInputError, match="output audit"):
        assess(report(), report(fingerprint))


@pytest.mark.parametrize("fingerprint", [None, "invalid"])
def test_invalid_source_evidence_rejected(fingerprint):
    with pytest.raises(RegressionInputError, match="valid fingerprint"):
        assess(report(fingerprint), report(OUTPUT))


def test_invalid_expected_output_fingerprint_rejected():
    with pytest.raises(RegressionInputError, match="valid trial output"):
        assess_audit_regressions(
            report(),
            report(OUTPUT),
            plan(),
            expected_output_fingerprint_sha256="invalid",
        )


def test_impossible_duplicate_count_rejected():
    with pytest.raises(RegressionInputError, match="Duplicate count"):
        assess(report(duplicates=11), report(OUTPUT))


def test_mutated_evidence_is_revalidated():
    before = report()
    before.row_count = -1
    with pytest.raises(ValidationError):
        assess(before, report(OUTPUT))


def test_inputs_are_not_modified():
    before, after, proposal = report(issues=[issue()]), report(OUTPUT), plan()
    snapshots = [obj.model_dump() for obj in (before, after, proposal)]
    assess(before, after, proposal)
    assert [obj.model_dump() for obj in (before, after, proposal)] == snapshots
