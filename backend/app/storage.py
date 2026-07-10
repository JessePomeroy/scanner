"""Storage helpers for uploaded scan packages."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import tempfile
from threading import Event
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

    The caller owns the source lifetime, and the job-specific destination must
    not already exist. Any read/write/publication/cancellation failure removes
    both temporary and newly published files.
    """
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination = destination.parent.resolve() / destination.name
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Upload destination already exists: {destination}")

    temporary_path: Path | None = None
    temporary_file = None
    published = Event()
    total_bytes = 0

    try:
        temporary_file = tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".part",
            delete=False,
        )
        temporary_path = Path(temporary_file.name)
        while True:
            chunk = await source.read(chunk_size)
            if not isinstance(chunk, bytes):
                raise TypeError("upload source must return bytes")
            if not chunk:
                break
            await _run_blocking_cancellation_safe(_write_chunk, temporary_file, chunk)
            total_bytes += len(chunk)

        await _run_blocking_cancellation_safe(_sync_and_close, temporary_file)
        temporary_file = None
        await _run_blocking_cancellation_safe(
            _publish_and_sync_directory,
            temporary_path,
            destination,
            published,
        )
        return total_bytes
    except BaseException:
        try:
            await _run_blocking_cancellation_safe(
                _cleanup_failed_upload,
                temporary_file,
                temporary_path,
                destination,
                published.is_set(),
            )
        except BaseException:
            # Cleanup is best-effort; never replace the primary storage error
            # or cancellation with a secondary close/unlink failure.
            pass
        raise


async def _run_blocking_cancellation_safe(operation, /, *args):
    """Run one sink operation off-loop and wait for its worker before cancelling."""
    task = asyncio.create_task(asyncio.to_thread(operation, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancellation:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        try:
            task.result()
        except BaseException:
            pass
        raise cancellation


def _sync_and_close(file) -> None:
    try:
        file.flush()
        os.fsync(file.fileno())
    finally:
        file.close()


def _write_chunk(file, chunk: bytes) -> None:
    remaining = memoryview(chunk)
    while remaining:
        written = file.write(remaining)
        if not isinstance(written, int) or written <= 0:
            raise OSError("Unable to make progress while writing upload")
        remaining = remaining[written:]


def _publish_and_sync_directory(
    temporary_path: Path,
    destination: Path,
    published: Event,
) -> None:
    os.replace(temporary_path, destination)
    published.set()
    _sync_directory(destination.parent)


def _cleanup_failed_upload(
    temporary_file,
    temporary_path: Path | None,
    destination: Path,
    published: bool,
) -> None:
    if temporary_file is not None:
        try:
            temporary_file.close()
        except OSError:
            pass
    if temporary_path is not None:
        temporary_path.unlink(missing_ok=True)
    if published:
        destination.unlink(missing_ok=True)
        _sync_directory(destination.parent)


def _sync_directory(path: Path) -> None:
    """Durably commit directory changes on POSIX, including macOS and WSL."""
    if os.name == "nt":
        # Native Windows does not expose portable directory fsync through
        # Python. The scanner's documented Windows backend runs under WSL.
        return

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
