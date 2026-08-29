"""Tests for deterministic file fingerprinting."""

from pathlib import Path

import pytest

from app.tools.fingerprint import calculate_sha256


def test_calculate_sha256_matches_known_fingerprint(tmp_path: Path) -> None:
    """Known file bytes should produce the expected SHA-256 value."""

    sample_file = tmp_path / "sample.csv"
    sample_file.write_bytes(b"abc")

    fingerprint = calculate_sha256(sample_file)

    assert fingerprint == ("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")


def test_fingerprint_changes_when_file_changes(tmp_path: Path) -> None:
    """Different file contents should produce different fingerprints."""

    sample_file = tmp_path / "sample.csv"
    sample_file.write_bytes(b"version one")
    first_fingerprint = calculate_sha256(sample_file)

    sample_file.write_bytes(b"version two")
    second_fingerprint = calculate_sha256(sample_file)

    assert first_fingerprint != second_fingerprint


def test_calculate_sha256_does_not_modify_source_file(tmp_path: Path) -> None:
    """Fingerprinting should leave the original file unchanged."""

    original_content = b"customer_id,value\n1,10\n"
    sample_file = tmp_path / "sample.csv"
    sample_file.write_bytes(original_content)

    calculate_sha256(sample_file)

    assert sample_file.read_bytes() == original_content


def test_calculate_sha256_raises_for_missing_file(tmp_path: Path) -> None:
    """A missing source file should raise a clear filesystem error."""

    missing_file = tmp_path / "missing.csv"

    with pytest.raises(FileNotFoundError):
        calculate_sha256(missing_file)
