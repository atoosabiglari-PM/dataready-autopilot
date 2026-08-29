"""Deterministic CSV auditing for the DataReady Autopilot trust gate."""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Literal

import pandas as pd
from pydantic import BaseModel, Field

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


def _normalize_column_name(column_name: str) -> str:
    """Normalize a column name for deterministic comparisons."""

    normalized = re.sub(r"[^a-z0-9]+", "_", column_name.strip().lower())
    return normalized.strip("_")


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


def audit_csv(file_path: str | Path) -> AuditReport:
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
            pii_match_count = int(
                nonempty_values.str.contains(
                    PII_VALUE_PATTERN,
                    na=False,
                ).sum()
            )

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
    )
