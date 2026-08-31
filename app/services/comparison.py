"""Deterministic before-and-after readiness comparison."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.tools.audit import AuditReport


class ReadinessComparison(BaseModel):
    """Evidence showing how a repaired dataset changed after re-audit."""

    before_status: str
    after_status: str

    before_quality_score: int = Field(ge=0, le=100)
    after_quality_score: int = Field(ge=0, le=100)
    quality_score_delta: int

    before_issue_count: int = Field(ge=0)
    after_issue_count: int = Field(ge=0)
    issue_count_delta: int

    before_row_count: int = Field(ge=0)
    after_row_count: int = Field(ge=0)
    row_count_delta: int

    before_duplicate_row_count: int = Field(ge=0)
    after_duplicate_row_count: int = Field(ge=0)
    duplicate_rows_removed: int = Field(ge=0)

    resolved_issue_codes: list[str] = Field(default_factory=list)
    remaining_issue_codes: list[str] = Field(default_factory=list)
    new_issue_codes: list[str] = Field(default_factory=list)


def build_readiness_comparison(
    before: AuditReport,
    after: AuditReport,
) -> ReadinessComparison:
    """Compare deterministic audit evidence before and after repair."""

    before_codes = {issue.code for issue in before.issues}
    after_codes = {issue.code for issue in after.issues}

    resolved_issue_codes = sorted(before_codes - after_codes)
    remaining_issue_codes = sorted(before_codes & after_codes)
    new_issue_codes = sorted(after_codes - before_codes)

    return ReadinessComparison(
        before_status=before.status,
        after_status=after.status,
        before_quality_score=before.quality_score,
        after_quality_score=after.quality_score,
        quality_score_delta=after.quality_score - before.quality_score,
        before_issue_count=len(before.issues),
        after_issue_count=len(after.issues),
        issue_count_delta=len(after.issues) - len(before.issues),
        before_row_count=before.row_count,
        after_row_count=after.row_count,
        row_count_delta=after.row_count - before.row_count,
        before_duplicate_row_count=before.duplicate_row_count,
        after_duplicate_row_count=after.duplicate_row_count,
        duplicate_rows_removed=max(
            0,
            before.duplicate_row_count - after.duplicate_row_count,
        ),
        resolved_issue_codes=resolved_issue_codes,
        remaining_issue_codes=remaining_issue_codes,
        new_issue_codes=new_issue_codes,
    )
