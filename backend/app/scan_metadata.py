"""Typed parsing for scan-package metadata contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import math
from pathlib import Path
from typing import Any


class ScanMetadataError(ValueError):
    """Raised when a metadata file does not match the package contract."""


@dataclass(frozen=True)
class FrameMetadata:
    id: int
    image: str
    timestamp: float
    resolution: tuple[int, int]
    image_source: str | None = None
    high_resolution_capture_failure: str | None = None


@dataclass(frozen=True)
class SessionMetadata:
    scan_id: str | None
    scan_mode: str | None
    image_count: int | None
    video_count: int | None
    object_center_world: tuple[float, float, float] | None
    object_radius_meters: float | None
    high_resolution_frame_capture_enabled: bool | None = None
    configured_video_resolution: tuple[int, int] | None = None
    high_resolution_image_count: int | None = None
    fallback_image_count: int | None = None


@dataclass(frozen=True)
class VideoMetadata:
    path: str
    captured_at: str
    duration_seconds: float | None
    frame_rate: float | None
    resolution: tuple[int, int] | None
    codec: str | None
    includes_audio: bool


@dataclass(frozen=True)
class ReconstructionScopeMetadata:
    schema_version: str
    mode: str
    mask_space: str
    mask_convention: str
    mask_count: int


@dataclass(frozen=True)
class ScanMetadata:
    frames: tuple[FrameMetadata, ...]
    session: SessionMetadata
    videos: tuple[VideoMetadata, ...]
    has_video_metadata: bool
    reconstruction_scope: ReconstructionScopeMetadata | None


def load_scan_metadata(metadata_dir: Path) -> ScanMetadata:
    """Load required metadata and validate scalar/collection field types."""
    frames_value = _read_json(metadata_dir / "frames.json")
    session_value = _read_json(metadata_dir / "session.json")
    video_path = metadata_dir / "video.json"
    video_value = _read_json(video_path) if video_path.exists() else []
    manifest_path = metadata_dir / "manifest.json"
    manifest_value = _read_json(manifest_path) if manifest_path.exists() else {}

    if not isinstance(frames_value, list):
        raise ScanMetadataError("frames.json must contain a JSON array")
    if not isinstance(session_value, dict):
        raise ScanMetadataError("session.json must contain a JSON object")
    if not isinstance(video_value, list):
        raise ScanMetadataError("video.json must contain a JSON array")
    if not isinstance(manifest_value, dict):
        raise ScanMetadataError("manifest.json must contain a JSON object")

    frames = tuple(_parse_frame(value, index) for index, value in enumerate(frames_value))
    videos = tuple(_parse_video(value, index) for index, value in enumerate(video_value))
    return ScanMetadata(
        frames=frames,
        session=_parse_session(session_value),
        videos=videos,
        has_video_metadata=video_path.exists(),
        reconstruction_scope=_parse_reconstruction_scope(manifest_value.get("reconstruction_scope")),
    )


def _parse_reconstruction_scope(value: Any) -> ReconstructionScopeMetadata | None:
    if value is None:
        return None
    item = _object(value, "manifest.json.reconstruction_scope")
    schema_version = _non_empty_string(item.get("schema_version"), "reconstruction_scope.schema_version")
    mode = _non_empty_string(item.get("mode"), "reconstruction_scope.mode")
    mask_space = _non_empty_string(item.get("mask_space"), "reconstruction_scope.mask_space")
    convention = _non_empty_string(item.get("mask_convention"), "reconstruction_scope.mask_convention")
    mask_count = _integer(item.get("mask_count"), "reconstruction_scope.mask_count", minimum=1)
    if schema_version != "1.0":
        raise ScanMetadataError("reconstruction_scope.schema_version must be '1.0'")
    if mode != "image_masks":
        raise ScanMetadataError("reconstruction_scope.mode must be 'image_masks'")
    if mask_space != "capture_image":
        raise ScanMetadataError("reconstruction_scope.mask_space must be 'capture_image'")
    if convention != "white_keep_black_exclude":
        raise ScanMetadataError(
            "reconstruction_scope.mask_convention must be 'white_keep_black_exclude'"
        )
    return ReconstructionScopeMetadata(schema_version, mode, mask_space, convention, mask_count)


def _parse_frame(value: Any, index: int) -> FrameMetadata:
    label = f"frames.json[{index}]"
    item = _object(value, label)
    frame_id = _integer(item.get("id"), f"{label}.id", minimum=0)
    image = _non_empty_string(item.get("image"), f"{label}.image")
    image_source = _optional_non_empty_string(
        item.get("image_source"),
        f"{label}.image_source",
    )
    capture_failure = _optional_non_empty_string(
        item.get("high_resolution_capture_failure"),
        f"{label}.high_resolution_capture_failure",
    )
    timestamp = _number(item.get("timestamp"), f"{label}.timestamp", minimum=0)
    resolution = _resolution(item.get("resolution"), f"{label}.resolution")
    return FrameMetadata(
        id=frame_id,
        image=image,
        image_source=image_source,
        high_resolution_capture_failure=capture_failure,
        timestamp=timestamp,
        resolution=resolution,
    )


def _parse_session(value: dict[str, Any]) -> SessionMetadata:
    scan_id = _optional_non_empty_string(value.get("scan_id"), "scan_id")
    scan_mode = _optional_non_empty_string(value.get("scan_mode"), "scan_mode")
    image_count = _optional_integer(value.get("image_count"), "image_count", minimum=0)
    video_count = _optional_integer(value.get("video_count"), "video_count", minimum=0)
    high_resolution_enabled = _optional_boolean(
        value.get("high_resolution_frame_capture_enabled"),
        "high_resolution_frame_capture_enabled",
    )
    configured_video_resolution = _optional_resolution(
        value.get("configured_video_resolution"),
        "configured_video_resolution",
    )
    high_resolution_image_count = _optional_integer(
        value.get("high_resolution_image_count"),
        "high_resolution_image_count",
        minimum=0,
    )
    fallback_image_count = _optional_integer(
        value.get("fallback_image_count"),
        "fallback_image_count",
        minimum=0,
    )
    object_center = _optional_number_tuple(
        value.get("object_center_world"),
        "object_center_world",
        length=3,
    )
    object_radius = _optional_number(
        value.get("object_radius_meters"),
        "object_radius_meters",
        exclusive_minimum=0,
    )

    created_at = value.get("created_at")
    if created_at is not None:
        _iso8601_string(created_at, "created_at")

    return SessionMetadata(
        scan_id=scan_id,
        scan_mode=scan_mode,
        image_count=image_count,
        video_count=video_count,
        high_resolution_frame_capture_enabled=high_resolution_enabled,
        configured_video_resolution=configured_video_resolution,
        high_resolution_image_count=high_resolution_image_count,
        fallback_image_count=fallback_image_count,
        object_center_world=object_center,
        object_radius_meters=object_radius,
    )


def _parse_video(value: Any, index: int) -> VideoMetadata:
    label = f"video.json[{index}]"
    item = _object(value, label)
    path = _non_empty_string(item.get("path"), f"{label}.path")
    captured_at = _iso8601_string(item.get("captured_at"), f"{label}.captured_at")
    duration = _optional_number(
        item.get("duration_seconds"),
        f"{label}.duration_seconds",
        exclusive_minimum=0,
    )
    frame_rate = _optional_number(
        item.get("frame_rate"),
        f"{label}.frame_rate",
        exclusive_minimum=0,
    )
    resolution = _optional_resolution(item.get("resolution"), f"{label}.resolution")
    codec = _optional_non_empty_string(item.get("codec"), f"{label}.codec")
    includes_audio = item.get("includes_audio")
    if not isinstance(includes_audio, bool):
        raise ScanMetadataError(f"{label}.includes_audio must be a boolean")

    return VideoMetadata(
        path=path,
        captured_at=captured_at,
        duration_seconds=duration,
        frame_rate=frame_rate,
        resolution=resolution,
        codec=codec,
        includes_audio=includes_audio,
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(
            path.read_text(),
            parse_constant=lambda value: _reject_non_finite_json_constant(path, value),
        )
    except FileNotFoundError:
        raise ScanMetadataError(f"Missing required metadata file: {path.name}") from None
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ScanMetadataError(f"Invalid JSON in {path}: {error}") from error


def _reject_non_finite_json_constant(path: Path, value: str) -> None:
    raise ScanMetadataError(f"Invalid JSON in {path}: non-finite number {value}")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ScanMetadataError(f"{label} must be an object")
    return value


def _non_empty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScanMetadataError(f"{label} must be a non-empty string")
    return value


def _optional_non_empty_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _non_empty_string(value, label)


def _integer(value: Any, label: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ScanMetadataError(f"{label} must be an integer greater than or equal to {minimum}")
    return value


def _optional_integer(value: Any, label: str, *, minimum: int) -> int | None:
    if value is None:
        return None
    return _integer(value, label, minimum=minimum)


def _optional_boolean(value: Any, label: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ScanMetadataError(f"{label} must be a boolean")
    return value


def _number(
    value: Any,
    label: str,
    *,
    minimum: float | None = None,
    exclusive_minimum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScanMetadataError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ScanMetadataError(f"{label} must be a finite number")
    if minimum is not None and result < minimum:
        raise ScanMetadataError(f"{label} must be greater than or equal to {minimum:g}")
    if exclusive_minimum is not None and result <= exclusive_minimum:
        raise ScanMetadataError(f"{label} must be greater than {exclusive_minimum:g}")
    return result


def _optional_number(
    value: Any,
    label: str,
    *,
    exclusive_minimum: float | None = None,
) -> float | None:
    if value is None:
        return None
    return _number(value, label, exclusive_minimum=exclusive_minimum)


def _resolution(value: Any, label: str) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2:
        raise ScanMetadataError(f"{label} must contain two positive integers")
    width = _integer(value[0], f"{label}[0]", minimum=1)
    height = _integer(value[1], f"{label}[1]", minimum=1)
    return (width, height)


def _optional_resolution(value: Any, label: str) -> tuple[int, int] | None:
    if value is None:
        return None
    return _resolution(value, label)


def _optional_number_tuple(
    value: Any,
    label: str,
    *,
    length: int,
) -> tuple[float, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != length:
        raise ScanMetadataError(f"{label} must be an array of {length} finite numbers")
    return tuple(_number(item, f"{label}[{index}]") for index, item in enumerate(value))


def _iso8601_string(value: Any, label: str) -> str:
    text = _non_empty_string(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ScanMetadataError(f"{label} must be an ISO 8601 timestamp with a UTC offset") from error
    if parsed.tzinfo is None:
        raise ScanMetadataError(f"{label} must be an ISO 8601 timestamp with a UTC offset")
    return text
