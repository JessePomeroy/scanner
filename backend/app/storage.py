"""Storage helpers for uploaded scan packages."""

from __future__ import annotations

from pathlib import Path
import zipfile


class UnsafeArchiveError(ValueError):
    """Raised when an archive entry would escape the extraction directory."""


def safe_extract_zip(zip_path: Path, destination: Path) -> None:
    """Extract a zip file while blocking path traversal entries."""
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as archive:
        for member in archive.infolist():
            target = (destination / member.filename).resolve()
            if target != destination and destination not in target.parents:
                raise UnsafeArchiveError(f"Unsafe archive member: {member.filename}")

        archive.extractall(destination)
