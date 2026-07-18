"""Strict post-capture representative-frame mask-authoring contract."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any, Literal

from app.scan_metadata import FrameMetadata


MaskOperation = Literal["keep", "erase"]
MASK_AUTHORING_FILENAME = "mask_authoring.json"
_MAX_BYTES = 1024 * 1024
_MAX_REPRESENTATIVE_FRAMES = 16
_MAX_REGIONS_PER_FRAME = 64
_MAX_POINTS_PER_REGION = 4096


class MaskAuthoringError(ValueError):
    """Raised when post-capture mask authoring is malformed or unsafe."""


@dataclass(frozen=True)
class MaskAuthoringPoint:
    x: float
    y: float

    def as_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y}


@dataclass(frozen=True)
class MaskAuthoringRegion:
    operation: MaskOperation
    points: tuple[MaskAuthoringPoint, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "points": [point.as_dict() for point in self.points],
        }


@dataclass(frozen=True)
class MaskAuthoringFrame:
    frame_id: int
    image: str
    regions: tuple[MaskAuthoringRegion, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "frame_id": self.frame_id,
            "image": self.image,
            "regions": [region.as_dict() for region in self.regions],
        }


@dataclass(frozen=True)
class MaskAuthoringPlan:
    revision: int
    representative_frames: tuple[MaskAuthoringFrame, ...]
    schema_version: str = "1.0"
    authoring_mode: str = "representative_frames"
    coordinate_space: str = "normalized_capture_image"
    mask_convention: str = "white_keep_black_exclude"

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "authoring_mode": self.authoring_mode,
            "coordinate_space": self.coordinate_space,
            "mask_convention": self.mask_convention,
            "revision": self.revision,
            "representative_frames": [frame.as_dict() for frame in self.representative_frames],
        }


def load_mask_authoring_plan(
    metadata_dir: Path,
    frames: tuple[FrameMetadata, ...],
) -> MaskAuthoringPlan | None:
    """Load an optional authoring plan and bind every selection to an exact frame."""
    path = metadata_dir / MASK_AUTHORING_FILENAME
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_BYTES:
        raise MaskAuthoringError(f"Mask-authoring file is missing or unsafe: {path}")
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda value: _reject_constant(value),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MaskAuthoringError(f"Mask-authoring JSON is invalid: {path}") from error
    plan = _parse_plan(payload)
    _validate_frame_associations(plan, frames)
    return plan


def representative_frame_indices(frame_count: int) -> tuple[int, ...]:
    """Return stable first/quartile/middle/three-quarter/last review samples."""
    if isinstance(frame_count, bool) or not isinstance(frame_count, int) or frame_count < 1:
        raise MaskAuthoringError("Frame count must be a positive integer")
    last = frame_count - 1
    return tuple(sorted({0, round(last * 0.25), round(last * 0.5), round(last * 0.75), last}))


def _parse_plan(value: object) -> MaskAuthoringPlan:
    item = _strict_object(
        value,
        "mask_authoring",
        {
            "schema_version", "authoring_mode", "coordinate_space", "mask_convention",
            "revision", "representative_frames",
        },
    )
    literals = {
        "schema_version": "1.0",
        "authoring_mode": "representative_frames",
        "coordinate_space": "normalized_capture_image",
        "mask_convention": "white_keep_black_exclude",
    }
    for name, expected in literals.items():
        if item[name] != expected:
            raise MaskAuthoringError(f"mask_authoring.{name} must be {expected!r}")
    revision = _integer(item["revision"], "mask_authoring.revision", minimum=1)
    values = _sequence(item["representative_frames"], "mask_authoring.representative_frames")
    if not 1 <= len(values) <= _MAX_REPRESENTATIVE_FRAMES:
        raise MaskAuthoringError(
            f"mask_authoring.representative_frames must contain 1-{_MAX_REPRESENTATIVE_FRAMES} frames"
        )
    return MaskAuthoringPlan(
        revision=revision,
        representative_frames=tuple(_parse_frame(frame, index) for index, frame in enumerate(values)),
    )


def _parse_frame(value: object, index: int) -> MaskAuthoringFrame:
    label = f"mask_authoring.representative_frames[{index}]"
    item = _strict_object(value, label, {"frame_id", "image", "regions"})
    frame_id = _integer(item["frame_id"], f"{label}.frame_id", minimum=0)
    image = item["image"]
    if not isinstance(image, str) or not image:
        raise MaskAuthoringError(f"{label}.image must be a non-empty string")
    regions = _sequence(item["regions"], f"{label}.regions")
    if not 1 <= len(regions) <= _MAX_REGIONS_PER_FRAME:
        raise MaskAuthoringError(
            f"{label}.regions must contain 1-{_MAX_REGIONS_PER_FRAME} regions"
        )
    parsed = tuple(_parse_region(region, label, region_index) for region_index, region in enumerate(regions))
    if not any(region.operation == "keep" for region in parsed):
        raise MaskAuthoringError(f"{label}.regions must contain at least one keep region")
    return MaskAuthoringFrame(frame_id=frame_id, image=image, regions=parsed)


def _parse_region(value: object, frame_label: str, index: int) -> MaskAuthoringRegion:
    label = f"{frame_label}.regions[{index}]"
    item = _strict_object(value, label, {"operation", "points"})
    operation = item["operation"]
    if operation not in {"keep", "erase"}:
        raise MaskAuthoringError(f"{label}.operation must be 'keep' or 'erase'")
    points = _sequence(item["points"], f"{label}.points")
    if not 3 <= len(points) <= _MAX_POINTS_PER_REGION:
        raise MaskAuthoringError(
            f"{label}.points must contain 3-{_MAX_POINTS_PER_REGION} points"
        )
    parsed = tuple(_parse_point(point, label, point_index) for point_index, point in enumerate(points))
    signed_double_area = sum(
        point.x * parsed[(index + 1) % len(parsed)].y
        - parsed[(index + 1) % len(parsed)].x * point.y
        for index, point in enumerate(parsed)
    )
    if abs(signed_double_area) <= math.ulp(1.0) * len(parsed):
        raise MaskAuthoringError(f"{label}.points form a degenerate polygon")
    return MaskAuthoringRegion(operation=operation, points=parsed)


def _parse_point(value: object, region_label: str, index: int) -> MaskAuthoringPoint:
    label = f"{region_label}.points[{index}]"
    item = _strict_object(value, label, {"x", "y"})
    return MaskAuthoringPoint(
        x=_unit_number(item["x"], f"{label}.x"),
        y=_unit_number(item["y"], f"{label}.y"),
    )


def _validate_frame_associations(
    plan: MaskAuthoringPlan,
    frames: tuple[FrameMetadata, ...],
) -> None:
    expected = {(frame.id, frame.image) for frame in frames}
    identities = [(frame.frame_id, frame.image) for frame in plan.representative_frames]
    if len(set(identities)) != len(identities):
        raise MaskAuthoringError("Mask-authoring representative frames must be unique")
    for identity in identities:
        if identity not in expected:
            raise MaskAuthoringError(
                f"Mask-authoring frame association does not exist in frames.json: {identity}"
            )


def _strict_object(value: object, label: str, fields: set[str]) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise MaskAuthoringError(f"{label} must be an object")
    actual = set(value)
    if actual != fields:
        missing = sorted(fields - actual)
        extra = sorted(actual - fields)
        raise MaskAuthoringError(f"{label} fields are invalid; missing={missing}, extra={extra}")
    return value


def _sequence(value: object, label: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, list):
        raise MaskAuthoringError(f"{label} must be an array")
    return value


def _integer(value: object, label: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise MaskAuthoringError(f"{label} must be an integer >= {minimum}")
    return value


def _unit_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MaskAuthoringError(f"{label} must be a finite number from 0 through 1")
    parsed = float(value)
    if not math.isfinite(parsed) or not 0 <= parsed <= 1:
        raise MaskAuthoringError(f"{label} must be a finite number from 0 through 1")
    return parsed


def _reject_constant(value: str) -> None:
    raise MaskAuthoringError(f"Mask-authoring JSON contains non-finite number {value}")
