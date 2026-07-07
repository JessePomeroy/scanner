"""Validate extracted scan packages before reconstruction starts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ScanValidationError(ValueError):
    """Raised when a scan package is incomplete or malformed."""


SUPPORTED_VIDEO_SUFFIXES = {".mov", ".mp4", ".m4v"}


@dataclass(frozen=True)
class ScanValidationReport:
    scan_dir: Path
    image_count: int
    frame_count: int
    video_count: int
    scan_id: str | None
    scan_mode: str | None
    object_center_world: list[float] | None
    object_radius_meters: float | None


def find_scan_root(extracted_dir: Path) -> Path:
    """Find the scan folder inside an extracted archive."""
    extracted_dir = extracted_dir.resolve()
    candidates = [extracted_dir]
    candidates.extend(path for path in extracted_dir.iterdir() if path.is_dir())

    for candidate in candidates:
        if (candidate / "images").is_dir() and (candidate / "metadata").is_dir():
            return candidate

    return extracted_dir


def validate_scan_package(scan_dir: Path) -> ScanValidationReport:
    """Validate the extracted scan package before reconstruction.

    Required structure:
    - images/
    - metadata/frames.json
    - metadata/session.json
    """
    scan_dir = scan_dir.resolve()
    images_dir = scan_dir / "images"
    metadata_dir = scan_dir / "metadata"
    frames_path = metadata_dir / "frames.json"
    session_path = metadata_dir / "session.json"

    missing = [
        path.relative_to(scan_dir)
        for path in [images_dir, frames_path, session_path]
        if not path.exists()
    ]
    if missing:
        formatted = ", ".join(str(path) for path in missing)
        raise ScanValidationError(f"Missing required scan package paths: {formatted}")

    if not images_dir.is_dir():
        raise ScanValidationError("images must be a directory")

    frames = _read_json_array(frames_path)
    session = _read_json_object(session_path)
    video_metadata_path = metadata_dir / "video.json"
    video_metadata = _read_json_array(video_metadata_path) if video_metadata_path.exists() else []
    images = sorted(
        path
        for path in images_dir.iterdir()
        if path.suffix.lower() in {".jpg", ".jpeg", ".heic", ".png"}
    )
    video_count = _video_file_count(scan_dir)

    if not images:
        raise ScanValidationError("images directory does not contain supported image files")

    if len(frames) != len(images):
        raise ScanValidationError(
            f"Frame metadata count ({len(frames)}) does not match image count ({len(images)})"
        )

    _validate_frame_image_references(scan_dir, frames)
    _validate_video_references(scan_dir, video_metadata)
    object_center_world = _optional_float_list(session, "object_center_world", expected_length=3)
    object_radius_meters = _optional_float(session, "object_radius_meters")

    if object_radius_meters is not None and object_radius_meters <= 0:
        raise ScanValidationError("object_radius_meters must be positive when present")

    return ScanValidationReport(
        scan_dir=scan_dir,
        image_count=len(images),
        frame_count=len(frames),
        video_count=video_count,
        scan_id=session.get("scan_id"),
        scan_mode=session.get("scan_mode"),
        object_center_world=object_center_world,
        object_radius_meters=object_radius_meters,
    )


def _read_json_array(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise ScanValidationError(f"Invalid JSON in {path}: {error}") from error

    if not isinstance(value, list):
        raise ScanValidationError(f"{path.name} must contain a JSON array")

    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ScanValidationError(f"{path.name}[{index}] must be an object")

    return value


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise ScanValidationError(f"Invalid JSON in {path}: {error}") from error

    if not isinstance(value, dict):
        raise ScanValidationError(f"{path.name} must contain a JSON object")

    return value


def _validate_frame_image_references(scan_dir: Path, frames: list[dict[str, Any]]) -> None:
    for index, frame in enumerate(frames):
        image = frame.get("image")
        if not isinstance(image, str) or not image:
            raise ScanValidationError(f"frames.json[{index}].image must be a non-empty string")

        image_path = (scan_dir / image).resolve()
        if scan_dir not in image_path.parents:
            raise ScanValidationError(f"frames.json[{index}].image escapes the scan directory")

        if not image_path.exists():
            raise ScanValidationError(f"Referenced image does not exist: {image}")

        if not image_path.is_file():
            raise ScanValidationError(f"Referenced image is not a file: {image}")


def _validate_video_references(scan_dir: Path, videos: list[dict[str, Any]]) -> None:
    for index, video in enumerate(videos):
        path = video.get("path")
        if not isinstance(path, str) or not path:
            raise ScanValidationError(f"video.json[{index}].path must be a non-empty string")

        video_path = (scan_dir / path).resolve()
        if scan_dir not in video_path.parents:
            raise ScanValidationError(f"video.json[{index}].path escapes the scan directory")

        if not video_path.exists():
            raise ScanValidationError(f"Referenced video does not exist: {path}")

        if not video_path.is_file():
            raise ScanValidationError(f"Referenced video is not a file: {path}")

        if video_path.suffix.lower() not in SUPPORTED_VIDEO_SUFFIXES:
            raise ScanValidationError(f"Referenced video has unsupported file type: {path}")


def _video_file_count(scan_dir: Path) -> int:
    video_dir = scan_dir / "video"
    if not video_dir.is_dir():
        return 0

    return len(
        [
            path
            for path in video_dir.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_VIDEO_SUFFIXES
        ]
    )


def _optional_float(value: dict[str, Any], key: str) -> float | None:
    raw = value.get(key)
    if raw is None:
        return None
    if not isinstance(raw, (int, float)):
        raise ScanValidationError(f"{key} must be a number when present")
    return float(raw)


def _optional_float_list(
    value: dict[str, Any],
    key: str,
    *,
    expected_length: int,
) -> list[float] | None:
    raw = value.get(key)
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) != expected_length:
        raise ScanValidationError(f"{key} must be an array of {expected_length} numbers")

    result: list[float] = []
    for index, item in enumerate(raw):
        if not isinstance(item, (int, float)):
            raise ScanValidationError(f"{key}[{index}] must be a number")
        result.append(float(item))

    return result
