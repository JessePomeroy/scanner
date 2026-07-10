"""Filesystem-backed reconstruction job lifecycle."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import tempfile
from threading import RLock

from app.schemas import JobRecord, JobStage, JobStatus


Clock = Callable[[], datetime]

_SCAN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_TERMINAL_STATUSES: set[JobStatus] = {"validated", "complete", "failed"}
_ALLOWED_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    "received": {"received", "processing", "failed"},
    "processing": {"processing", "validated", "complete", "failed"},
    "validated": {"validated"},
    "complete": {"complete"},
    "failed": {"failed"},
}
_ACTIVE_STAGES: set[JobStage] = {
    "queued",
    "validating",
    "reconstructing",
    "meshing",
    "exporting",
}
_ALLOWED_STAGE_TRANSITIONS: dict[JobStage, set[JobStage]] = {
    "received": {"queued", "validating"},
    "queued": {"queued", "validating"},
    "validating": {"validating", "reconstructing"},
    "reconstructing": {"reconstructing", "meshing", "exporting"},
    "meshing": {"meshing", "exporting"},
    "exporting": {"exporting"},
    "finished": set(),
}


class JobTransitionError(ValueError):
    """Raised when a job is moved backward or restarted after completion."""


class InvalidScanIDError(ValueError):
    """Raised when a scan id cannot safely name a job record."""


class JobStore:
    """Own job transitions, timestamps, ordering, and atomic JSON persistence."""

    def __init__(self, jobs_dir: Path, *, clock: Clock | None = None) -> None:
        self.jobs_dir = jobs_dir
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = RLock()
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

    def create(self, scan_id: str) -> JobRecord:
        with self._lock:
            timestamp = self._now()
            record = JobRecord(
                scan_id=scan_id,
                status="received",
                stage="received",
                created_at=timestamp,
                updated_at=timestamp,
            )
            self._write(record)
            return record

    def update(
        self,
        scan_id: str,
        *,
        status: JobStatus,
        stage: JobStage | None = None,
        message: str | None = None,
        image_count: int | None = None,
        frame_count: int | None = None,
        outputs: dict[str, str] | None = None,
    ) -> JobRecord:
        with self._lock:
            current = self.read(scan_id)
            if status not in _ALLOWED_TRANSITIONS[current.status]:
                raise JobTransitionError(
                    f"Cannot move job {scan_id} from {current.status} to {status}"
                )

            next_stage = self._next_stage(current, status=status, stage=stage)
            timestamp = self._now()
            values = _dump_model(current)
            values.update(
                {
                    "status": status,
                    "stage": next_stage,
                    "message": message,
                    "image_count": (
                        image_count if image_count is not None else current.image_count
                    ),
                    "frame_count": (
                        frame_count if frame_count is not None else current.frame_count
                    ),
                    "outputs": outputs if outputs is not None else current.outputs,
                    "created_at": current.created_at or timestamp,
                    "updated_at": timestamp,
                    "started_at": (
                        current.started_at or timestamp
                        if status == "processing"
                        else current.started_at
                    ),
                    "finished_at": (
                        current.finished_at or timestamp
                        if status in _TERMINAL_STATUSES
                        else None
                    ),
                }
            )
            record = JobRecord(**values)
            self._write(record)
            return record

    def read(self, scan_id: str) -> JobRecord:
        with self._lock:
            path = self._record_path(scan_id)
            if not path.exists():
                raise KeyError(scan_id)

            raw = path.read_text(encoding="utf-8")
            if hasattr(JobRecord, "model_validate_json"):
                return JobRecord.model_validate_json(raw)
            return JobRecord.parse_raw(raw)

    def list(self, *, limit: int = 50) -> list[JobRecord]:
        with self._lock:
            return self._list_records(limit=limit)

    def list_active(self) -> list[JobRecord]:
        """Return every non-terminal job for startup reconciliation."""
        with self._lock:
            return [
                record
                for record in self._list_records(limit=None)
                if record.status in {"received", "processing"}
            ]

    def _list_records(self, *, limit: int | None) -> list[JobRecord]:
        records: list[tuple[float, JobRecord]] = []
        for path in self.jobs_dir.glob("*.json"):
            try:
                record = self.read(path.stem)
                order = _timestamp_order(record.updated_at, fallback=path.stat().st_mtime)
                records.append((order, record))
            except Exception:
                continue

        records.sort(key=lambda item: item[0], reverse=True)
        ordered = [record for _, record in records]
        return ordered if limit is None else ordered[:limit]

    def _write(self, record: JobRecord) -> None:
        path = self._record_path(record.scan_id)
        if hasattr(record, "model_dump_json"):
            payload = record.model_dump_json(indent=2)
        else:
            payload = record.json(indent=2)

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.jobs_dir,
                prefix=f".{record.scan_id}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(payload)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())

            os.replace(temporary_path, path)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise

    def _record_path(self, scan_id: str) -> Path:
        if not _SCAN_ID_PATTERN.fullmatch(scan_id):
            raise InvalidScanIDError(f"Invalid scan id: {scan_id!r}")
        return self.jobs_dir / f"{scan_id}.json"

    def _next_stage(
        self,
        current: JobRecord,
        *,
        status: JobStatus,
        stage: JobStage | None,
    ) -> JobStage | None:
        if status in _TERMINAL_STATUSES:
            if status == current.status:
                return "finished"
            if status == "validated" and current.stage not in {"validating", None}:
                raise JobTransitionError(
                    f"Cannot validate job {current.scan_id} from stage {current.stage}"
                )
            if status == "complete" and current.stage not in {"exporting", None}:
                raise JobTransitionError(
                    f"Cannot complete job {current.scan_id} from stage {current.stage}"
                )
            return "finished"

        if status != "processing":
            return current.stage

        next_stage = stage
        if next_stage is None and current.status == "processing":
            next_stage = current.stage
        if next_stage not in _ACTIVE_STAGES:
            raise JobTransitionError("Processing jobs require an active lifecycle stage")

        current_stage = current.stage
        if current.status == "processing" and current_stage is None:
            return next_stage
        if current_stage is None or next_stage not in _ALLOWED_STAGE_TRANSITIONS[current_stage]:
            raise JobTransitionError(
                f"Cannot move job {current.scan_id} from stage {current_stage} "
                f"to {next_stage}"
            )
        return next_stage

    def _now(self) -> str:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("JobStore clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc).isoformat()


def _dump_model(record: JobRecord) -> dict:
    if hasattr(record, "model_dump"):
        return record.model_dump()
    return record.dict()


def _timestamp_order(value: str | None, *, fallback: float) -> float:
    if value is None:
        return fallback
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return fallback
