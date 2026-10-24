"""Bounded and private conversion of uploaded tabular data to CSV.

Only normalized CSV enters DataReady's existing governed engine. Files are
created inside a per-request TemporaryDirectory and never persisted or logged.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

from fastapi import UploadFile

from app.tools.preflight import (
    MAX_CELL_LENGTH,
    MAX_COLUMNS,
    MAX_FILE_SIZE_BYTES,
    MAX_ROWS,
)

SUPPORTED_EXTENSIONS = frozenset({".csv", ".json", ".xlsx"})
MAX_XLSX_EXPANDED_BYTES = 64 * 1024 * 1024


class CustomerUploadError(ValueError):
    """User-provided data is unsupported or exceeds the agreed safety limits."""


def _scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        result = "true" if value else "false"
    elif isinstance(value, (str, int, float)):
        result = str(value)
    else:
        raise CustomerUploadError(
            "Only scalar text, number, boolean, or null values are supported."
        )
    if len(result) > MAX_CELL_LENGTH:
        raise CustomerUploadError("A value exceeds the allowed cell length.")
    return result


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise CustomerUploadError("JSON objects cannot contain duplicate keys.")
        obj[key] = value
    return obj


def _convert_json(source: Path, destination: Path) -> None:
    try:
        payload = json.loads(
            source.read_text(encoding="utf-8-sig"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                CustomerUploadError("Non-finite numeric JSON values are unsupported.")
            ),
        )
    except (ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise CustomerUploadError("The uploaded JSON could not be safely parsed.") from error

    if isinstance(payload, dict) and set(payload) == {"records"}:
        payload = payload["records"]
    if not isinstance(payload, list) or not payload:
        raise CustomerUploadError(
            "JSON must be a nonempty array of flat objects or {\"records\": [...]}."
        )
    if len(payload) > MAX_ROWS:
        raise CustomerUploadError("The JSON dataset exceeds the row limit.")

    headers: list[str] = []
    for record in payload:
        if not isinstance(record, dict) or not record:
            raise CustomerUploadError("Every JSON row must be a nonempty flat object.")
        for key, value in record.items():
            if not isinstance(key, str) or not key or len(key) > MAX_CELL_LENGTH:
                raise CustomerUploadError("JSON column names must be nonempty text.")
            _scalar(value)
            if key not in headers:
                headers.append(key)
                if len(headers) > MAX_COLUMNS:
                    raise CustomerUploadError("The JSON dataset exceeds the column limit.")

    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        for record in payload:
            writer.writerow([_scalar(record.get(key)) for key in headers])
    if destination.stat().st_size > MAX_FILE_SIZE_BYTES:
        raise CustomerUploadError("The converted CSV exceeds the safety size limit.")


def _convert_excel(source: Path, destination: Path) -> None:
    try:
        with ZipFile(source) as archive:
            infos = archive.infolist()
            expanded = sum(item.file_size for item in infos)
            if len(infos) > 1500 or expanded > MAX_XLSX_EXPANDED_BYTES:
                raise CustomerUploadError("The workbook exceeds safe processing limits.")
            if any(
                item.file_size > MAX_XLSX_EXPANDED_BYTES // 2
                or (item.compress_size == 0 and item.file_size > 0)
                or (item.compress_size > 0 and item.file_size > item.compress_size * 150)
                for item in infos
            ):
                raise CustomerUploadError("The workbook has unsafe compression.")
    except (BadZipFile, OSError) as error:
        raise CustomerUploadError("The XLSX workbook is unreadable.") from error

    try:
        from openpyxl import load_workbook
    except ImportError as error:
        raise CustomerUploadError("Excel support is not installed on this server.") from error

    workbook = None
    try:
        workbook = load_workbook(
            source, read_only=True, data_only=False, keep_links=False
        )
        if not workbook.worksheets:
            raise CustomerUploadError("The workbook contains no worksheets.")
        worksheet = workbook.worksheets[0]
        if worksheet.max_column and worksheet.max_column > MAX_COLUMNS:
            raise CustomerUploadError("The worksheet exceeds the column limit.")
        if worksheet.max_row and worksheet.max_row > MAX_ROWS + 1:
            raise CustomerUploadError("The worksheet exceeds the row limit.")

        with destination.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            count = 0
            for cells in worksheet.iter_rows():
                count += 1
                if count > MAX_ROWS + 1:
                    raise CustomerUploadError("The worksheet exceeds the row limit.")
                if len(cells) > MAX_COLUMNS:
                    raise CustomerUploadError("The worksheet exceeds the column limit.")
                row: list[str] = []
                for cell in cells:
                    if getattr(cell, "data_type", None) == "f":
                        raise CustomerUploadError(
                            "Formula cells must be converted to values before upload."
                        )
                    value = cell.value
                    if value is not None and not isinstance(value, (str, int, float, bool)):
                        value = str(value)
                    row.append(_scalar(value))
                writer.writerow(row)

        if destination.stat().st_size > MAX_FILE_SIZE_BYTES:
            raise CustomerUploadError("The converted worksheet exceeds the size limit.")
    except CustomerUploadError:
        raise
    except Exception as error:
        raise CustomerUploadError("The XLSX workbook could not be safely read.") from error
    finally:
        if workbook is not None:
            workbook.close()


async def receive_customer_dataset(
    upload: UploadFile, directory: Path
) -> Path:
    """Receive a bounded upload, normalize if needed, and return a local CSV.

    Never use the supplied filename as a filesystem path. All work is isolated
    in a request-specific temporary directory. Caller owns its lifetime.
    """
    filename = upload.filename or ""
    if not filename or len(filename) > 255:
        raise CustomerUploadError("Select a CSV, JSON, or XLSX dataset.")
    extension = Path(filename).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise CustomerUploadError("Accepted formats: .csv, .json, and .xlsx.")

    incoming = directory / ("original" + extension)
    size = 0
    try:
        with incoming.open("xb") as handle:
            while True:
                data = await upload.read(1024 * 1024)
                if not data:
                    break
                size += len(data)
                if size > MAX_FILE_SIZE_BYTES:
                    raise CustomerUploadError("Upload exceeds the 10 MB file limit.")
                handle.write(data)
    finally:
        await upload.close()

    if size == 0:
        raise CustomerUploadError("The uploaded file is empty.")

    if extension == ".csv":
        return incoming

    destination = directory / "normalized.csv"
    if extension == ".json":
        _convert_json(incoming, destination)
    else:
        _convert_excel(incoming, destination)
    return destination
