"""Safety checks performed before a CSV enters the audit pipeline."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024
MAX_ROWS = 100_000
MAX_COLUMNS = 200
MAX_CELL_LENGTH = 10_000
SAMPLE_SIZE_BYTES = 64 * 1024

BINARY_SIGNATURES = {
    b"PK\x03\x04": "ZIP or Office archive",
    b"\x1f\x8b": "GZIP archive",
    b"%PDF-": "PDF document",
    b"\xd0\xcf\x11\xe0": "Microsoft Office binary document",
    b"Salted__": "encrypted OpenSSL content",
    b"\x89PNG": "PNG image",
    b"\xff\xd8\xff": "JPEG image",
}


class PreflightReport(BaseModel):
    """Structured evidence produced by the CSV preflight gate."""

    status: Literal["ACCEPTED", "BLOCKED"]
    file_name: str
    file_size_bytes: int = Field(ge=0)
    encoding: str | None = None
    delimiter: str | None = None
    column_count: int | None = Field(default=None, ge=0)
    data_row_count: int | None = Field(default=None, ge=0)
    risk_flags: list[str] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)


def _blocked_report(
    path: Path,
    file_size: int,
    risk_flag: str,
    message: str,
    *,
    encoding: str | None = None,
    delimiter: str | None = None,
    column_count: int | None = None,
    data_row_count: int | None = None,
) -> PreflightReport:
    """Create a consistent report for a blocked file."""

    return PreflightReport(
        status="BLOCKED",
        file_name=path.name,
        file_size_bytes=file_size,
        encoding=encoding,
        delimiter=delimiter,
        column_count=column_count,
        data_row_count=data_row_count,
        risk_flags=[risk_flag],
        messages=[message],
    )


def preflight_csv(
    file_path: str | Path,
    *,
    max_file_size_bytes: int = MAX_FILE_SIZE_BYTES,
    max_rows: int = MAX_ROWS,
    max_columns: int = MAX_COLUMNS,
    max_cell_length: int = MAX_CELL_LENGTH,
) -> PreflightReport:
    """Determine whether a CSV is safe enough to enter the audit pipeline."""

    path = Path(file_path)

    if not path.exists():
        return _blocked_report(
            path,
            0,
            "FILE_NOT_FOUND",
            "The requested file does not exist.",
        )

    if not path.is_file():
        return _blocked_report(
            path,
            0,
            "NOT_A_FILE",
            "The supplied path does not identify a regular file.",
        )

    file_size = path.stat().st_size

    if path.suffix.lower() != ".csv":
        return _blocked_report(
            path,
            file_size,
            "UNSUPPORTED_FILE_TYPE",
            "Only files with the .csv extension are accepted.",
        )

    if file_size == 0:
        return _blocked_report(
            path,
            file_size,
            "EMPTY_FILE",
            "The CSV is empty and contains no header or data rows.",
        )

    if file_size > max_file_size_bytes:
        return _blocked_report(
            path,
            file_size,
            "FILE_TOO_LARGE",
            f"The file exceeds the safety limit of {max_file_size_bytes} bytes.",
        )

    with path.open("rb") as binary_file:
        sample_bytes = binary_file.read(SAMPLE_SIZE_BYTES)

    for signature, description in BINARY_SIGNATURES.items():
        if sample_bytes.startswith(signature):
            return _blocked_report(
                path,
                file_size,
                "BINARY_OR_ENCRYPTED_CONTENT",
                f"The file appears to contain {description}, not readable CSV text.",
            )

    if b"\x00" in sample_bytes:
        return _blocked_report(
            path,
            file_size,
            "BINARY_OR_ENCRYPTED_CONTENT",
            "The file contains null bytes and is not permitted as readable CSV text.",
        )

    try:
        sample_text = sample_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return _blocked_report(
            path,
            file_size,
            "UNSUPPORTED_ENCODING",
            "The file is not valid UTF-8 or UTF-8 with a byte-order mark.",
        )

    if not sample_text.strip():
        return _blocked_report(
            path,
            file_size,
            "EMPTY_FILE",
            "The CSV contains only whitespace.",
            encoding="utf-8",
        )

    try:
        detected_dialect = csv.Sniffer().sniff(
            sample_text,
            delimiters=",;\t|",
        )
        detected_delimiter = detected_dialect.delimiter
    except csv.Error:
        detected_delimiter = "," if "," in sample_text else None

    if detected_delimiter not in {None, ","}:
        return _blocked_report(
            path,
            file_size,
            "NON_COMMA_DELIMITER",
            f"The detected delimiter {detected_delimiter!r} is not supported.",
            encoding="utf-8",
            delimiter=detected_delimiter,
        )

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as csv_file:
            reader = csv.reader(csv_file, delimiter=",", strict=True)

            try:
                header = next(reader)
            except StopIteration:
                return _blocked_report(
                    path,
                    file_size,
                    "EMPTY_FILE",
                    "The CSV contains no readable header.",
                    encoding="utf-8",
                    delimiter=",",
                )

            if not header or all(not cell.strip() for cell in header):
                return _blocked_report(
                    path,
                    file_size,
                    "MISSING_HEADER",
                    "The CSV does not contain a usable header.",
                    encoding="utf-8",
                    delimiter=",",
                )

            column_count = len(header)

            if column_count > max_columns:
                return _blocked_report(
                    path,
                    file_size,
                    "TOO_MANY_COLUMNS",
                    f"The CSV exceeds the safety limit of {max_columns} columns.",
                    encoding="utf-8",
                    delimiter=",",
                    column_count=column_count,
                )

            if any(len(cell) > max_cell_length for cell in header):
                return _blocked_report(
                    path,
                    file_size,
                    "CELL_TOO_LARGE",
                    f"A header cell exceeds the limit of {max_cell_length} characters.",
                    encoding="utf-8",
                    delimiter=",",
                    column_count=column_count,
                )

            data_row_count = 0

            for physical_row_number, row in enumerate(reader, start=2):
                if not row or all(not cell.strip() for cell in row):
                    continue

                data_row_count += 1

                if data_row_count > max_rows:
                    return _blocked_report(
                        path,
                        file_size,
                        "TOO_MANY_ROWS",
                        f"The CSV exceeds the safety limit of {max_rows} data rows.",
                        encoding="utf-8",
                        delimiter=",",
                        column_count=column_count,
                        data_row_count=data_row_count,
                    )

                if len(row) != column_count:
                    return _blocked_report(
                        path,
                        file_size,
                        "MALFORMED_CSV",
                        (
                            f"Physical row {physical_row_number} has {len(row)} fields; "
                            f"the header has {column_count}."
                        ),
                        encoding="utf-8",
                        delimiter=",",
                        column_count=column_count,
                        data_row_count=data_row_count,
                    )

                if any(len(cell) > max_cell_length for cell in row):
                    return _blocked_report(
                        path,
                        file_size,
                        "CELL_TOO_LARGE",
                        (
                            f"Physical row {physical_row_number} contains a cell exceeding "
                            f"{max_cell_length} characters."
                        ),
                        encoding="utf-8",
                        delimiter=",",
                        column_count=column_count,
                        data_row_count=data_row_count,
                    )

    except UnicodeDecodeError:
        return _blocked_report(
            path,
            file_size,
            "UNSUPPORTED_ENCODING",
            "The complete file could not be decoded safely as UTF-8.",
            encoding="utf-8",
            delimiter=",",
        )
    except csv.Error as error:
        return _blocked_report(
            path,
            file_size,
            "MALFORMED_CSV",
            f"The CSV parser rejected the file: {error}.",
            encoding="utf-8",
            delimiter=",",
        )

    if data_row_count == 0:
        return _blocked_report(
            path,
            file_size,
            "HEADER_ONLY_FILE",
            "The CSV contains a header but no data rows.",
            encoding="utf-8",
            delimiter=",",
            column_count=column_count,
            data_row_count=0,
        )

    return PreflightReport(
        status="ACCEPTED",
        file_name=path.name,
        file_size_bytes=file_size,
        encoding="utf-8",
        delimiter=",",
        column_count=column_count,
        data_row_count=data_row_count,
        messages=["The CSV passed the preflight safety checks."],
    )
