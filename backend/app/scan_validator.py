"""Validate extracted scan packages before reconstruction starts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ScanValidationError(ValueError):
    """Raised when a scan package is incomplete or malformed."""


@dataclass(frozen=True)
class ScanValidationReport:
    scan_dir: Path
    image_count: int
    frame_count: int
    scan_id: str | None


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
    images = sorted(
        path
        for path in images_dir.iterdir()
        if path.suffix.lower() in {".jpg", ".jpeg", ".heic", ".png"}
    )

    if not images:
        raise ScanValidationError("images directory does not contain supported image files")

    if len(frames) != len(images):
        raise ScanValidationError(
            f"Frame metadata count ({len(frames)}) does not match image count ({len(images)})"
        )

    _validate_frame_image_references(scan_dir, frames)

    return ScanValidationReport(
        scan_dir=scan_dir,
        image_count=len(images),
        frame_count=len(frames),
        scan_id=session.get("scan_id"),
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
