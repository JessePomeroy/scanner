"""Storage helpers for uploaded scan packages."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
from typing import Protocol
import zipfile


class UnsafeArchiveError(ValueError):
    """Raised when an archive entry would escape the extraction directory."""


class AsyncBinaryReader(Protocol):
    """Minimal async source contract used for bounded upload persistence."""

    async def read(self, size: int = -1) -> bytes: ...


async def store_upload_atomically(
    source: AsyncBinaryReader,
    destination: Path,
    *,
    chunk_size: int = 1024 * 1024,
) -> int:
    """Stream an upload to a sibling temporary file, then replace atomically.

    The caller owns the source lifetime. Any read/write/cancellation failure
    removes the temporary file and leaves an existing destination unchanged.
    """
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination = destination.parent.resolve() / destination.name
    temporary_path: Path | None = None
    total_bytes = 0

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".part",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            while True:
                chunk = await source.read(chunk_size)
                if not isinstance(chunk, bytes):
                    raise TypeError("upload source must return bytes")
                if not chunk:
                    break
                temporary_file.write(chunk)
                total_bytes += len(chunk)

            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        os.replace(temporary_path, destination)
        return total_bytes
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


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
