"""Explicit, scoped business context supplied by the person reviewing a dataset."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

ColumnName = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class ReviewChoices(BaseModel):
    """No blanket risk override; each decision names an existing column."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    public_contact_columns: tuple[ColumnName, ...] = Field(default=(), max_length=200)
    optional_columns: tuple[ColumnName, ...] = Field(default=(), max_length=200)


class AuditReview(ReviewChoices):
    """Server-recorded review context; never sent to the reasoning model."""

    reviewer_reference: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=10, max_length=500)
    reviewed_at: int = Field(ge=0)
