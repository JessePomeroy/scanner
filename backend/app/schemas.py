"""API response schemas."""

from __future__ import annotations

import json
from typing import Literal

try:
    from pydantic import BaseModel, Field
except ModuleNotFoundError:
    # Repo-level tests exercise storage helpers without installing backend deps.
    # FastAPI runtime installs Pydantic through backend/requirements.txt.
    class BaseModel:
        def __init__(self, **values: object) -> None:
            annotations = getattr(self.__class__, "__annotations__", {})
            for name in annotations:
                if name in values:
                    continue

                default = getattr(self.__class__, name, None)
                setattr(self, name, default() if callable(default) else default)

            for name, value in values.items():
                setattr(self, name, value)

        def dict(self) -> dict[str, object]:
            return {
                name: value
                for name, value in self.__dict__.items()
                if not name.startswith("_")
            }

        def json(self, indent: int | None = None) -> str:
            return json.dumps(self.dict(), indent=indent)

        @classmethod
        def parse_raw(cls, raw: str) -> "BaseModel":
            return cls(**json.loads(raw))

    def Field(*, default_factory):
        return default_factory


JobStatus = Literal["received", "processing", "validated", "complete", "failed"]
JobStage = Literal[
    "received",
    "queued",
    "validating",
    "reconstructing",
    "meshing",
    "exporting",
    "finished",
]


class JobRecord(BaseModel):
    scan_id: str
    status: JobStatus
    stage: JobStage | None = None
    message: str | None = None
    image_count: int | None = None
    frame_count: int | None = None
    outputs: dict[str, str] = Field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
