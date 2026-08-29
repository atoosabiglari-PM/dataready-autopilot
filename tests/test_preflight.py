"""Tests for the CSV preflight safety gate."""

from pathlib import Path

from app.tools.preflight import preflight_csv


def test_accepts_valid_comma_separated_utf8_csv(tmp_path: Path) -> None:
    csv_file = tmp_path / "valid.csv"
    csv_file.write_text(
        "name,age\nAtoosa,47\nSam,35\n",
        encoding="utf-8",
    )

    report = preflight_csv(csv_file)

    assert report.status == "ACCEPTED"
    assert report.encoding == "utf-8"
    assert report.delimiter == ","
    assert report.column_count == 2
    assert report.data_row_count == 2
    assert report.risk_flags == []


def test_blocks_empty_file(tmp_path: Path) -> None:
    csv_file = tmp_path / "empty.csv"
    csv_file.write_bytes(b"")

    report = preflight_csv(csv_file)

    assert report.status == "BLOCKED"
    assert report.risk_flags == ["EMPTY_FILE"]


def test_blocks_header_only_file(tmp_path: Path) -> None:
    csv_file = tmp_path / "header_only.csv"
    csv_file.write_text("name,age\n", encoding="utf-8")

    report = preflight_csv(csv_file)

    assert report.status == "BLOCKED"
    assert report.risk_flags == ["HEADER_ONLY_FILE"]


def test_blocks_non_comma_delimiter(tmp_path: Path) -> None:
    csv_file = tmp_path / "semicolon.csv"
    csv_file.write_text(
        "name;age\nAtoosa;47\n",
        encoding="utf-8",
    )

    report = preflight_csv(csv_file)

    assert report.status == "BLOCKED"
    assert report.risk_flags == ["NON_COMMA_DELIMITER"]


def test_blocks_inconsistent_row_structure(tmp_path: Path) -> None:
    csv_file = tmp_path / "malformed.csv"
    csv_file.write_text(
        "name,age\nAtoosa,47,unexpected\n",
        encoding="utf-8",
    )

    report = preflight_csv(csv_file)

    assert report.status == "BLOCKED"
    assert report.risk_flags == ["MALFORMED_CSV"]


def test_blocks_unsupported_encoding(tmp_path: Path) -> None:
    csv_file = tmp_path / "unsupported_encoding.csv"
    csv_file.write_bytes(b"name,city\nAtoosa,\xff\n")

    report = preflight_csv(csv_file)

    assert report.status == "BLOCKED"
    assert report.risk_flags == ["UNSUPPORTED_ENCODING"]


def test_blocks_archive_or_encrypted_looking_content(tmp_path: Path) -> None:
    csv_file = tmp_path / "encrypted.csv"
    csv_file.write_bytes(b"Salted__encrypted-content")

    report = preflight_csv(csv_file)

    assert report.status == "BLOCKED"
    assert report.risk_flags == ["BINARY_OR_ENCRYPTED_CONTENT"]


def test_blocks_file_over_configured_size_limit(tmp_path: Path) -> None:
    csv_file = tmp_path / "large.csv"
    csv_file.write_text("name,age\nAtoosa,47\n", encoding="utf-8")

    report = preflight_csv(csv_file, max_file_size_bytes=5)

    assert report.status == "BLOCKED"
    assert report.risk_flags == ["FILE_TOO_LARGE"]


def test_blocks_too_many_rows(tmp_path: Path) -> None:
    csv_file = tmp_path / "too_many_rows.csv"
    csv_file.write_text(
        "name,age\nAtoosa,47\nSam,35\n",
        encoding="utf-8",
    )

    report = preflight_csv(csv_file, max_rows=1)

    assert report.status == "BLOCKED"
    assert report.risk_flags == ["TOO_MANY_ROWS"]


def test_blocks_too_many_columns(tmp_path: Path) -> None:
    csv_file = tmp_path / "too_many_columns.csv"
    csv_file.write_text(
        "name,age,city\nAtoosa,47,Palo Alto\n",
        encoding="utf-8",
    )

    report = preflight_csv(csv_file, max_columns=2)

    assert report.status == "BLOCKED"
    assert report.risk_flags == ["TOO_MANY_COLUMNS"]


def test_blocks_oversized_cell(tmp_path: Path) -> None:
    csv_file = tmp_path / "oversized_cell.csv"
    csv_file.write_text(
        "id,value\n1,abcdef\n",
        encoding="utf-8",
    )

    report = preflight_csv(csv_file, max_cell_length=5)

    assert report.status == "BLOCKED"
    assert report.risk_flags == ["CELL_TOO_LARGE"]
