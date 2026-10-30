"""Deterministic CSV auditing for the DataReady Autopilot trust gate."""

from __future__ import annotations

import csv
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

import pandas as pd
from pydantic import BaseModel, Field

from app.core.audit_review import AuditReview
from app.tools.fingerprint import calculate_sha256
from app.tools.preflight import PreflightReport, preflight_csv

TYPE_SAMPLE_LIMIT = 2_000

PII_COLUMN_NAMES = {
    "address",
    "date_of_birth",
    "dob",
    "driver_license",
    "email",
    "email_address",
    "first_name",
    "full_name",
    "last_name",
    "name",
    "passport",
    "phone",
    "phone_number",
    "social_security_number",
    "ssn",
}

PII_VALUE_PATTERN = re.compile(
    r"(?:\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b)"
    r"|(?:\b\d{3}-\d{2}-\d{4}\b)"
    r"|(?:(?<!\d)(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)"
    r"\d{3}[-.\s]?\d{4}(?!\d))",
    re.IGNORECASE,
)

PUBLIC_CONTACT_COLUMNS = {
    "public_phone": "phone", "public_phone_number": "phone",
    "business_phone": "phone", "business_phone_number": "phone",
    "public_email": "email", "public_email_address": "email",
    "business_email": "email", "business_email_address": "email",
}
EMAIL_VALUE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
PHONE_VALUE = re.compile(
    r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}"
    r"(?:\s*(?:ext\.?|x)\s*\d{1,6})?", re.IGNORECASE,
)
SSN_VALUE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
COORDINATE_LIMITS = {
    "latitude": 90, "lat": 90, "center_latitude": 90,
    "longitude": 180, "lon": 180, "lng": 180, "center_longitude": 180,
}
DECIMAL_VALUE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")

PROMPT_INJECTION_PATTERN = re.compile(
    r"(?:\b(?:ignore|disregard|override)\s+(?:all\s+)?"
    r"(?:previous|prior|system)\s+instructions?\b)"
    r"|(?:\breveal\s+(?:the\s+)?system\s+prompt\b)"
    r"|(?:\byou\s+are\s+(?:now\s+)?(?:chatgpt|an?\s+ai)\b)"
    r"|(?:\bdeveloper\s+message\b)"
    r"|(?:\bsystem\s+prompt\b)",
    re.IGNORECASE,
)

PENALTIES = {
    "INFO": 2,
    "WARNING": 10,
    "CRITICAL": 25,
}


class AuditIssue(BaseModel):
    """One deterministic finding from the CSV audit."""

    code: str
    severity: Literal["INFO", "WARNING", "CRITICAL"]
    message: str
    column: str | None = None
    count: int = Field(default=1, ge=1)


class AuditReport(BaseModel):
    """Evidence-backed result of the deterministic CSV audit."""

    status: Literal["READY", "QUARANTINED", "BLOCKED"]
    file_name: str
    fingerprint_sha256: str | None
    row_count: int = Field(ge=0)
    column_count: int = Field(ge=0)
    duplicate_row_count: int = Field(ge=0)
    quality_score: int = Field(ge=0, le=100)
    preflight: PreflightReport
    issues: list[AuditIssue] = Field(default_factory=list)
    reviewed_findings: list[AuditIssue] = Field(default_factory=list)
    review_context: AuditReview | None = None


def _normalize_column_name(column_name: str) -> str:
    """Normalize a column name for deterministic comparisons."""

    normalized = re.sub(r"[^a-z0-9]+", "_", column_name.strip().lower())
    return normalized.strip("_")


def public_contact_kind(column_name: str) -> str | None:
    return PUBLIC_CONTACT_COLUMNS.get(_normalize_column_name(column_name))


def _valid_coordinate(value: str, column_name: str) -> bool:
    """A typed, in-range whole numeric cell cannot be a partial phone match.

    This does not exempt emails, phone strings, SSNs, or arbitrary contents just
    because someone named the column Longitude.
    """
    limit = COORDINATE_LIMITS.get(_normalize_column_name(column_name))
    if limit is None or not DECIMAL_VALUE.fullmatch(value):
        return False
    try:
        number = Decimal(value)
        return number.is_finite() and -limit <= number <= limit
    except InvalidOperation:
        return False


def _apply_review(
    dataframe: pd.DataFrame, issues: list[AuditIssue], review: AuditReview,
) -> tuple[list[AuditIssue], list[AuditIssue]]:
    """Recheck the declaration against actual values on every fresh audit."""
    public = set(review.public_contact_columns)
    optional = set(review.optional_columns)
    if len(public) != len(review.public_contact_columns) or len(optional) != len(review.optional_columns):
        raise ValueError("Review columns must be unique.")
    if not (public | optional) or not (public | optional) <= set(dataframe.columns):
        raise ValueError("Review must name existing columns.")
    if any(issue.code == "AMBIGUOUS_COLUMN_NAMES" for issue in issues):
        raise ValueError("Resolve ambiguous column names in the source before reviewing.")
    for column in public:
        kind = public_contact_kind(column)
        if kind is None:
            raise ValueError("Only explicitly public/business phone or email columns can be classified.")
        pattern = PHONE_VALUE if kind == "phone" else EMAIL_VALUE
        values = dataframe[column].dropna().astype(str).str.strip()
        for value in values:
            if SSN_VALUE.search(value) or (PII_VALUE_PATTERN.search(value) and not pattern.fullmatch(value)):
                raise ValueError("A declared contact column contains incompatible sensitive patterns.")
    accepted, remaining = [], []
    for issue in issues:
        covered = (
            issue.column in public and issue.code in {"PII_COLUMN_NAME", "PII_VALUE_PATTERN"}
        ) or (issue.column in optional and issue.code == "MISSING_VALUES")
        (accepted if covered else remaining).append(issue)
    return remaining, accepted


def _blocked_audit_report(
    path: Path,
    fingerprint: str | None,
    preflight: PreflightReport,
    code: str,
    message: str,
) -> AuditReport:
    """Create an audit report for a file that cannot be audited safely."""

    return AuditReport(
        status="BLOCKED",
        file_name=path.name,
        fingerprint_sha256=fingerprint,
        row_count=0,
        column_count=0,
        duplicate_row_count=0,
        quality_score=0,
        preflight=preflight,
        issues=[
            AuditIssue(
                code=code,
                severity="CRITICAL",
                message=message,
            )
        ],
    )


def audit_csv(file_path: str | Path, *, review_context: AuditReview | None = None) -> AuditReport:
    """Audit a CSV without modifying its contents."""

    path = Path(file_path)
    fingerprint = calculate_sha256(path) if path.is_file() else None
    preflight = preflight_csv(path)

    if preflight.status == "BLOCKED":
        risk_code = preflight.risk_flags[0] if preflight.risk_flags else "PREFLIGHT_BLOCKED"
        message = (
            preflight.messages[0]
            if preflight.messages
            else "The file failed the preflight safety gate."
        )
        return _blocked_audit_report(
            path,
            fingerprint,
            preflight,
            risk_code,
            message,
        )

    try:
        dataframe = pd.read_csv(
            path,
            dtype=str,
            encoding="utf-8-sig",
            keep_default_na=True,
        )
    except (OSError, UnicodeError, pd.errors.ParserError, ValueError) as error:
        return _blocked_audit_report(
            path,
            fingerprint,
            preflight,
            "CSV_READ_FAILURE",
            f"The deterministic audit could not read the CSV safely: {error}.",
        )

    issues: list[AuditIssue] = []
    row_count = len(dataframe.index)
    column_count = len(dataframe.columns)

    with path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        raw_header = next(csv.reader(csv_file, delimiter=",", strict=True))

    normalized_headers = [_normalize_column_name(column_name) for column_name in raw_header]

    suspicious_headers = [
        column_name
        for column_name, normalized_name in zip(
            raw_header,
            normalized_headers,
            strict=True,
        )
        if not normalized_name or normalized_name.startswith("unnamed")
    ]

    if suspicious_headers:
        issues.append(
            AuditIssue(
                code="SUSPICIOUS_COLUMN_NAME",
                severity="WARNING",
                message=("One or more columns have blank or automatically generated names."),
                count=len(suspicious_headers),
            )
        )

    duplicate_normalized_headers = {
        normalized_name
        for normalized_name in normalized_headers
        if normalized_name and normalized_headers.count(normalized_name) > 1
    }

    if duplicate_normalized_headers:
        issues.append(
            AuditIssue(
                code="AMBIGUOUS_COLUMN_NAMES",
                severity="CRITICAL",
                message=("Multiple columns become identical after name normalization."),
                count=len(duplicate_normalized_headers),
            )
        )

    blank_or_missing = dataframe.isna() | dataframe.fillna("").apply(
        lambda column: column.astype(str).str.strip().eq("")
    )

    for column_name in dataframe.columns:
        missing_count = int(blank_or_missing[column_name].sum())

        if missing_count:
            issues.append(
                AuditIssue(
                    code="MISSING_VALUES",
                    severity="WARNING",
                    message="The column contains missing or whitespace-only values.",
                    column=str(column_name),
                    count=missing_count,
                )
            )

    duplicate_row_count = int(dataframe.duplicated(keep="first").sum())

    if duplicate_row_count:
        issues.append(
            AuditIssue(
                code="DUPLICATE_ROWS",
                severity="WARNING",
                message="The CSV contains exact duplicate data rows.",
                count=duplicate_row_count,
            )
        )

    for column_name in dataframe.columns:
        normalized_name = _normalize_column_name(str(column_name))
        nonempty_values = dataframe[column_name].dropna().astype(str).str.strip()
        nonempty_values = nonempty_values[nonempty_values.ne("")]

        if normalized_name in PII_COLUMN_NAMES:
            issues.append(
                AuditIssue(
                    code="PII_COLUMN_NAME",
                    severity="CRITICAL",
                    message=(
                        "The column name indicates that it may contain "
                        "personally identifiable information."
                    ),
                    column=str(column_name),
                )
            )

        if not nonempty_values.empty:
            matches = nonempty_values.str.contains(PII_VALUE_PATTERN, na=False)
            if _normalize_column_name(str(column_name)) in COORDINATE_LIMITS:
                valid_coordinates = nonempty_values.map(
                    lambda value, column=str(column_name): _valid_coordinate(value, column)
                )
                matches &= ~valid_coordinates
                invalid_count = int((~valid_coordinates).sum())
                if invalid_count:
                    issues.append(AuditIssue(
                        code="INVALID_COORDINATE", severity="WARNING", column=str(column_name),
                        count=invalid_count,
                        message="The coordinate column contains nonnumeric or out-of-range values. Latitude must be within -90 to 90; longitude within -180 to 180.",
                    ))
            pii_match_count = int(matches.sum())

            if pii_match_count:
                issues.append(
                    AuditIssue(
                        code="PII_VALUE_PATTERN",
                        severity="CRITICAL",
                        message=(
                            "Values matching an email, phone, or Social Security "
                            "number pattern were detected. Values are not included "
                            "in this report."
                        ),
                        column=str(column_name),
                        count=pii_match_count,
                    )
                )

            prompt_injection_count = int(
                nonempty_values.str.contains(
                    PROMPT_INJECTION_PATTERN,
                    na=False,
                ).sum()
            )

            if prompt_injection_count:
                issues.append(
                    AuditIssue(
                        code="PROMPT_INJECTION_PATTERN",
                        severity="CRITICAL",
                        message=(
                            "Text resembling an attempt to manipulate AI instructions "
                            "was detected. The source values are not included."
                        ),
                        column=str(column_name),
                        count=prompt_injection_count,
                    )
                )

            type_sample = nonempty_values.head(TYPE_SAMPLE_LIMIT)
            numeric_mask = pd.to_numeric(
                type_sample,
                errors="coerce",
            ).notna()

            if numeric_mask.any() and (~numeric_mask).any():
                issues.append(
                    AuditIssue(
                        code="MIXED_NUMERIC_TEXT",
                        severity="WARNING",
                        message=(
                            "The sampled values contain a mixture of numeric "
                            "and nonnumeric content."
                        ),
                        column=str(column_name),
                        count=len(type_sample),
                    )
                )

    reviewed_findings: list[AuditIssue] = []
    if review_context is not None:
        issues, reviewed_findings = _apply_review(dataframe, issues, review_context)
    total_penalty = sum(PENALTIES[issue.severity] for issue in issues)
    quality_score = max(0, 100 - total_penalty)
    status: Literal["READY", "QUARANTINED"] = "READY" if not issues else "QUARANTINED"

    return AuditReport(
        status=status,
        file_name=path.name,
        fingerprint_sha256=fingerprint,
        row_count=row_count,
        column_count=column_count,
        duplicate_row_count=duplicate_row_count,
        quality_score=quality_score,
        preflight=preflight,
        issues=issues,
        reviewed_findings=reviewed_findings,
        review_context=review_context,
    )
