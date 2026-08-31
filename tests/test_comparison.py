"""Tests for deterministic before-and-after readiness comparison."""

from app.services.comparison import build_readiness_comparison
from app.tools.audit import AuditIssue, AuditReport
from app.tools.preflight import PreflightReport


def make_preflight(file_name: str) -> PreflightReport:
    """Create a representative accepted preflight report."""

    return PreflightReport(
        status="ACCEPTED",
        file_name=file_name,
        file_size_bytes=1024,
        encoding="utf-8",
        delimiter=",",
        column_count=2,
        data_row_count=10,
        risk_flags=[],
        messages=[],
    )


def test_builds_before_after_readiness_comparison() -> None:
    before = AuditReport(
        status="QUARANTINED",
        file_name="source.csv",
        fingerprint_sha256="a" * 64,
        row_count=10,
        column_count=2,
        duplicate_row_count=2,
        quality_score=70,
        preflight=make_preflight("source.csv"),
        issues=[
            AuditIssue(
                code="DUPLICATE_ROWS",
                severity="WARNING",
                message="Duplicates detected.",
                count=2,
            ),
            AuditIssue(
                code="OUTER_WHITESPACE",
                severity="WARNING",
                message="Whitespace detected.",
                column="name",
                count=3,
            ),
            AuditIssue(
                code="MIXED_NUMERIC_TEXT",
                severity="WARNING",
                message="Mixed values detected.",
                column="amount",
                count=4,
            ),
        ],
    )

    after = AuditReport(
        status="QUARANTINED",
        file_name="repaired.csv",
        fingerprint_sha256="b" * 64,
        row_count=8,
        column_count=2,
        duplicate_row_count=0,
        quality_score=90,
        preflight=make_preflight("repaired.csv"),
        issues=[
            AuditIssue(
                code="MIXED_NUMERIC_TEXT",
                severity="WARNING",
                message="Mixed values remain.",
                column="amount",
                count=4,
            )
        ],
    )

    result = build_readiness_comparison(
        before,
        after,
    )

    assert result.before_status == "QUARANTINED"
    assert result.after_status == "QUARANTINED"

    assert result.before_quality_score == 70
    assert result.after_quality_score == 90
    assert result.quality_score_delta == 20

    assert result.before_issue_count == 3
    assert result.after_issue_count == 1
    assert result.issue_count_delta == -2

    assert result.before_row_count == 10
    assert result.after_row_count == 8
    assert result.row_count_delta == -2

    assert result.before_duplicate_row_count == 2
    assert result.after_duplicate_row_count == 0
    assert result.duplicate_rows_removed == 2

    assert result.resolved_issue_codes == [
        "DUPLICATE_ROWS",
        "OUTER_WHITESPACE",
    ]

    assert result.remaining_issue_codes == [
        "MIXED_NUMERIC_TEXT",
    ]

    assert result.new_issue_codes == []


def test_comparison_detects_new_post_repair_issue() -> None:
    before = AuditReport(
        status="QUARANTINED",
        file_name="source.csv",
        fingerprint_sha256="a" * 64,
        row_count=10,
        column_count=2,
        duplicate_row_count=0,
        quality_score=90,
        preflight=make_preflight("source.csv"),
        issues=[
            AuditIssue(
                code="OUTER_WHITESPACE",
                severity="WARNING",
                message="Whitespace detected.",
                column="name",
                count=2,
            )
        ],
    )

    after = AuditReport(
        status="QUARANTINED",
        file_name="repaired.csv",
        fingerprint_sha256="b" * 64,
        row_count=10,
        column_count=2,
        duplicate_row_count=0,
        quality_score=80,
        preflight=make_preflight("repaired.csv"),
        issues=[
            AuditIssue(
                code="NEW_TEST_ISSUE",
                severity="WARNING",
                message="A new issue appeared.",
                column="name",
                count=1,
            )
        ],
    )

    result = build_readiness_comparison(
        before,
        after,
    )

    assert result.quality_score_delta == -10
    assert result.resolved_issue_codes == [
        "OUTER_WHITESPACE",
    ]
    assert result.remaining_issue_codes == []
    assert result.new_issue_codes == [
        "NEW_TEST_ISSUE",
    ]
