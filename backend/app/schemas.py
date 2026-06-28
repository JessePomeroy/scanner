"""API response schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


JobStatus = Literal["received", "processing", "validated", "complete", "failed"]


class JobRecord(BaseModel):
    scan_id: str
    status: JobStatus
    message: str | None = None
    image_count: int | None = None
    frame_count: int | None = None
    outputs: dict[str, str] = {}
