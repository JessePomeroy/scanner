"""Coordinate incoming upload persistence with reconstruction job state."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal, Protocol

from app.storage import AsyncBinaryReader, store_upload_atomically


class UploadJobStore(Protocol):
    def update(
        self,
        scan_id: str,
        *,
        status: Literal["failed"],
        message: str,
    ) -> object: ...


async def store_job_upload(
    source: AsyncBinaryReader,
    destination: Path,
    *,
    scan_id: str,
    jobs: UploadJobStore,
) -> int:
    """Store one upload and record a terminal job best-effort on failure."""
    try:
        return await store_upload_atomically(source, destination)
    except asyncio.CancelledError:
        _record_failure_best_effort(
            jobs,
            scan_id,
            "Upload was interrupted before the scan package was stored.",
        )
        raise
    except Exception as error:
        _record_failure_best_effort(
            jobs,
            scan_id,
            f"Unable to store uploaded scan package: {error}",
        )
        raise


def _record_failure_best_effort(
    jobs: UploadJobStore,
    scan_id: str,
    message: str,
) -> None:
    try:
        jobs.update(scan_id, status="failed", message=message)
    except BaseException:
        # Preserve the storage exception or cancellation. JobStore already
        # preserves its last valid record when a write fails.
        pass
