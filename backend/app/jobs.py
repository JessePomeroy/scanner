"""Small filesystem-backed job status store."""

from __future__ import annotations

import json
from pathlib import Path

from app.schemas import JobRecord, JobStatus


class JobStore:
    def __init__(self, jobs_dir: Path) -> None:
        self.jobs_dir = jobs_dir
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

    def create(self, scan_id: str) -> JobRecord:
        record = JobRecord(scan_id=scan_id, status="received")
        self.write(record)
        return record

    def update(
        self,
        scan_id: str,
        *,
        status: JobStatus,
        message: str | None = None,
        image_count: int | None = None,
        frame_count: int | None = None,
        outputs: dict[str, str] | None = None,
    ) -> JobRecord:
        current = self.read(scan_id)
        values = _dump_model(current)
        values.update(
            {
                "status": status,
                "message": message,
                "image_count": image_count if image_count is not None else current.image_count,
                "frame_count": frame_count if frame_count is not None else current.frame_count,
                "outputs": outputs if outputs is not None else current.outputs,
            }
        )
        record = JobRecord(**values)
        self.write(record)
        return record

    def read(self, scan_id: str) -> JobRecord:
        path = self.jobs_dir / f"{scan_id}.json"
        if not path.exists():
            raise KeyError(scan_id)

        raw = path.read_text()
        if hasattr(JobRecord, "model_validate_json"):
            return JobRecord.model_validate_json(raw)
        return JobRecord.parse_raw(raw)

    def write(self, record: JobRecord) -> None:
        path = self.jobs_dir / f"{record.scan_id}.json"
        if hasattr(record, "model_dump_json"):
            path.write_text(record.model_dump_json(indent=2))
        else:
            path.write_text(record.json(indent=2))


def _dump_model(record: JobRecord) -> dict:
    if hasattr(record, "model_dump"):
        return record.model_dump()
    return record.dict()
