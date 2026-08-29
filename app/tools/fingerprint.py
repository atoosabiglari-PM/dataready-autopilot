"""Utilities for calculating file fingerprints."""

from hashlib import sha256
from pathlib import Path

CHUNK_SIZE_BYTES = 1024 * 1024


def calculate_sha256(file_path: str | Path) -> str:
    """Calculate and return the SHA-256 fingerprint of a file."""

    path = Path(file_path)
    digest = sha256()

    with path.open("rb") as source_file:
        while True:
            chunk = source_file.read(CHUNK_SIZE_BYTES)

            if not chunk:
                break

            digest.update(chunk)

    return digest.hexdigest()
