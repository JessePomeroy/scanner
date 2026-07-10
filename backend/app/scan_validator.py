"""Validate extracted scan packages before reconstruction starts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.scan_metadata import (
    FrameMetadata,
    ScanMetadataError,
    VideoMetadata,
    load_scan_metadata,
)


class ScanValidationError(ValueError):
    """Raised when a scan package is incomplete or malformed."""


SUPPORTED_VIDEO_SUFFIXES = {".mov", ".mp4", ".m4v"}
SUPPORTED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".heic", ".png"}
OPTIONAL_CAPTURE_DIRECTORIES = ("video", "depth", "arkit", "preview")


@dataclass(frozen=True)
class ScanValidationReport:
    scan_dir: Path
    image_count: int
    frame_count: int
    video_count: int
    video_metadata_count: int
    scan_id: str | None
    scan_mode: str | None
    object_center_world: list[float] | None
    object_radius_meters: float | None
    session_image_count: int | None
    session_video_count: int | None
    integrity_warnings: tuple[str, ...]


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

    _validate_owned_directory(scan_dir, images_dir, label="images")
    _validate_owned_directory(scan_dir, metadata_dir, label="metadata")
    image_files = _validated_flat_files(images_dir, label="images")
    _validated_flat_files(metadata_dir, label="metadata")
    capture_files: dict[str, list[Path]] = {}
    for name in OPTIONAL_CAPTURE_DIRECTORIES:
        capture_path = scan_dir / name
        if capture_path.exists() or capture_path.is_symlink():
            _validate_owned_directory(scan_dir, capture_path, label=name)
            capture_files[name] = _validated_flat_files(capture_path, label=name)

    try:
        metadata = load_scan_metadata(metadata_dir)
    except ScanMetadataError as error:
        raise ScanValidationError(str(error)) from error

    images = _supported_files(image_files, SUPPORTED_IMAGE_SUFFIXES)
    video_files = _supported_files(capture_files.get("video", []), SUPPORTED_VIDEO_SUFFIXES)

    if not images:
        raise ScanValidationError("images directory does not contain supported image files")

    if len(metadata.frames) != len(images):
        raise ScanValidationError(
            f"Frame metadata count ({len(metadata.frames)}) does not match image count ({len(images)})"
        )

    _validate_frame_image_references(scan_dir, metadata.frames, images)
    _validate_session_counts(
        image_count=len(images),
        video_count=len(video_files),
        session_image_count=metadata.session.image_count,
        session_video_count=metadata.session.video_count,
    )
    video_references = _validate_video_references(scan_dir, metadata.videos)
    actual_video_paths = {path.resolve() for path in video_files}
    integrity_warnings: list[str] = []

    if metadata.has_video_metadata:
        unreferenced_videos = actual_video_paths - video_references
        if unreferenced_videos:
            names = ", ".join(
                str(path.relative_to(scan_dir)) for path in sorted(unreferenced_videos)
            )
            raise ScanValidationError(f"Video files without metadata references: {names}")
    elif actual_video_paths:
        integrity_warnings.append("video_metadata_missing")

    return ScanValidationReport(
        scan_dir=scan_dir,
        image_count=len(images),
        frame_count=len(metadata.frames),
        video_count=len(video_files),
        video_metadata_count=len(metadata.videos),
        scan_id=metadata.session.scan_id,
        scan_mode=metadata.session.scan_mode,
        object_center_world=(
            list(metadata.session.object_center_world)
            if metadata.session.object_center_world is not None
            else None
        ),
        object_radius_meters=metadata.session.object_radius_meters,
        session_image_count=metadata.session.image_count,
        session_video_count=metadata.session.video_count,
        integrity_warnings=tuple(integrity_warnings),
    )


def _validate_frame_image_references(
    scan_dir: Path,
    frames: tuple[FrameMetadata, ...],
    images: list[Path],
) -> None:
    frame_ids: set[int] = set()
    referenced_images: set[Path] = set()
    previous_timestamp: float | None = None
    for index, frame in enumerate(frames):
        if frame.id in frame_ids:
            raise ScanValidationError(f"Duplicate frame id in frames.json: {frame.id}")
        frame_ids.add(frame.id)

        image_path = _validate_package_file_reference(
            scan_dir,
            frame.image,
            label=f"frames.json[{index}].image",
            directory="images",
            suffixes=SUPPORTED_IMAGE_SUFFIXES,
        )
        if image_path in referenced_images:
            raise ScanValidationError(f"Duplicate image reference in frames.json: {frame.image}")
        referenced_images.add(image_path)

        if previous_timestamp is not None and frame.timestamp <= previous_timestamp:
            raise ScanValidationError(
                f"frames.json timestamps must increase; index {index} is not after index {index - 1}"
            )
        previous_timestamp = frame.timestamp

    actual_images = {path.resolve() for path in images}
    unreferenced_images = actual_images - referenced_images
    if unreferenced_images:
        names = ", ".join(
            str(path.relative_to(scan_dir)) for path in sorted(unreferenced_images)
        )
        raise ScanValidationError(f"Image files without frame metadata references: {names}")


def _validate_video_references(
    scan_dir: Path,
    videos: tuple[VideoMetadata, ...],
) -> set[Path]:
    referenced_videos: set[Path] = set()
    for index, video in enumerate(videos):
        video_path = _validate_package_file_reference(
            scan_dir,
            video.path,
            label=f"video.json[{index}].path",
            directory="video",
            suffixes=SUPPORTED_VIDEO_SUFFIXES,
        )
        if video_path in referenced_videos:
            raise ScanValidationError(f"Duplicate video reference in video.json: {video.path}")
        referenced_videos.add(video_path)
    return referenced_videos


def _validate_package_file_reference(
    scan_dir: Path,
    relative_path: str,
    *,
    label: str,
    directory: str,
    suffixes: set[str],
) -> Path:
    if not relative_path.startswith(f"{directory}/"):
        raise ScanValidationError(f"{label} must be inside the {directory} directory")
    if "\\" in relative_path:
        raise ScanValidationError(f"{label} must use forward slashes")

    relative = Path(relative_path)
    if relative.parent != Path(directory):
        raise ScanValidationError(f"{label} must be a direct child of the {directory} directory")

    package_path = scan_dir / relative_path
    if package_path.is_symlink():
        raise ScanValidationError(f"{label} must not be a symbolic link")

    file_path = package_path.resolve()
    expected_dir = (scan_dir / directory).resolve()
    if expected_dir not in file_path.parents:
        raise ScanValidationError(f"{label} escapes the {directory} directory")
    if not file_path.exists():
        raise ScanValidationError(f"Referenced file does not exist: {relative_path}")
    if not file_path.is_file():
        raise ScanValidationError(f"Referenced path is not a file: {relative_path}")
    if file_path.suffix.lower() not in suffixes:
        raise ScanValidationError(f"Referenced file has unsupported type: {relative_path}")
    return file_path


def _validate_owned_directory(scan_dir: Path, path: Path, *, label: str) -> None:
    if path.is_symlink():
        raise ScanValidationError(f"{label} must not be a symbolic link")
    if not path.is_dir():
        raise ScanValidationError(f"{label} must be a directory")

    resolved = path.resolve()
    if scan_dir not in resolved.parents:
        raise ScanValidationError(f"{label} directory escapes the scan directory")


def _validated_flat_files(directory: Path, *, label: str) -> list[Path]:
    files: list[Path] = []
    root = directory.resolve()
    for path in directory.iterdir():
        if path.is_symlink():
            raise ScanValidationError(f"{label} must not contain symbolic links: {path.name}")
        if path.is_dir():
            raise ScanValidationError(f"{label} must not contain nested directories: {path.name}")
        if not path.is_file():
            raise ScanValidationError(f"{label} contains an unsupported filesystem entry: {path.name}")

        resolved = path.resolve()
        if root not in resolved.parents:
            raise ScanValidationError(f"{label} entry escapes its directory: {path.name}")
        files.append(path)
    return sorted(files)


def _supported_files(files: list[Path], suffixes: set[str]) -> list[Path]:
    return [path for path in files if path.suffix.lower() in suffixes]


def _validate_session_counts(
    *,
    image_count: int,
    video_count: int,
    session_image_count: int | None,
    session_video_count: int | None,
) -> None:
    if session_image_count is not None and session_image_count != image_count:
        raise ScanValidationError(
            f"session.json image_count ({session_image_count}) does not match image files ({image_count})"
        )
    if session_video_count is not None and session_video_count != video_count:
        raise ScanValidationError(
            f"session.json video_count ({session_video_count}) does not match video files ({video_count})"
        )
