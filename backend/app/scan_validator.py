"""Validate extracted scan packages before reconstruction starts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from app.scan_metadata import (
    FrameMetadata,
    ScanMetadata,
    ScanMetadataError,
    VideoMetadata,
    load_scan_metadata,
)
from app.mask_processor import MaskValidationError, validate_capture_mask_png
from app.mask_authoring import (
    MaskAuthoringError,
    load_mask_authoring_plan,
    representative_frame_indices,
)


class ScanValidationError(ValueError):
    """Raised when a scan package is incomplete or malformed."""


SUPPORTED_VIDEO_SUFFIXES = {".mov", ".mp4", ".m4v"}
SUPPORTED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".heic", ".png"}
SUPPORTED_KEYFRAME_IMAGE_SOURCES = {
    "arkit_high_resolution",
    "arkit_video_frame_fallback",
}
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
    high_resolution_frame_capture_enabled: bool | None
    configured_video_resolution: tuple[int, int] | None
    high_resolution_image_count: int | None
    fallback_image_count: int | None
    integrity_warnings: tuple[str, ...]
    reconstruction_scope: dict[str, object] | None
    capture_mask_count: int
    mask_authoring: dict[str, object] | None


def find_scan_root(extracted_dir: Path) -> Path:
    """Find the scan folder inside an extracted archive."""
    extracted_dir = extracted_dir.resolve()
    candidates = [extracted_dir]
    for path in extracted_dir.iterdir():
        if path.is_symlink() or not path.is_dir():
            continue
        resolved = path.resolve()
        if extracted_dir not in resolved.parents:
            continue
        candidates.append(resolved)

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
    try:
        mask_authoring_plan = load_mask_authoring_plan(metadata_dir, metadata.frames)
    except MaskAuthoringError as error:
        raise ScanValidationError(str(error)) from error
    capture_mask_count = _validate_capture_masks(
        scan_dir,
        metadata,
        has_mask_authoring=mask_authoring_plan is not None,
    )
    _validate_session_counts(
        image_count=len(images),
        video_count=len(video_files),
        session_image_count=metadata.session.image_count,
        session_video_count=metadata.session.video_count,
    )
    _validate_image_capture_evidence(metadata)
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
        high_resolution_frame_capture_enabled=(
            metadata.session.high_resolution_frame_capture_enabled
        ),
        configured_video_resolution=metadata.session.configured_video_resolution,
        high_resolution_image_count=metadata.session.high_resolution_image_count,
        fallback_image_count=metadata.session.fallback_image_count,
        integrity_warnings=tuple(integrity_warnings),
        reconstruction_scope=(
            {
                "schema_version": metadata.reconstruction_scope.schema_version,
                "mode": metadata.reconstruction_scope.mode,
                "mask_space": metadata.reconstruction_scope.mask_space,
                "mask_convention": metadata.reconstruction_scope.mask_convention,
                "mask_count": metadata.reconstruction_scope.mask_count,
            }
            if metadata.reconstruction_scope is not None
            else None
        ),
        capture_mask_count=capture_mask_count,
        mask_authoring=(
            mask_authoring_plan.as_dict() if mask_authoring_plan is not None else None
        ),
    )


def _validate_image_capture_evidence(metadata: ScanMetadata) -> None:
    session = metadata.session
    session_values = (
        session.high_resolution_frame_capture_enabled,
        session.configured_video_resolution,
        session.high_resolution_image_count,
        session.fallback_image_count,
    )
    frame_has_evidence = any(
        frame.image_source is not None
        or frame.high_resolution_capture_failure is not None
        for frame in metadata.frames
    )
    if not frame_has_evidence and all(value is None for value in session_values):
        return
    if any(value is None for value in session_values):
        raise ScanValidationError(
            "High-resolution capture session evidence is incomplete"
        )
    if any(frame.image_source is None for frame in metadata.frames):
        raise ScanValidationError(
            "Every frame must declare image_source when high-resolution evidence is present"
        )

    high_resolution_count = 0
    fallback_count = 0
    for index, frame in enumerate(metadata.frames):
        if frame.image_source not in SUPPORTED_KEYFRAME_IMAGE_SOURCES:
            raise ScanValidationError(
                f"frames.json[{index}].image_source is unsupported"
            )
        if frame.image_source == "arkit_high_resolution":
            high_resolution_count += 1
            if frame.high_resolution_capture_failure is not None:
                raise ScanValidationError(
                    f"frames.json[{index}] high-resolution image cannot declare a fallback reason"
                )
        else:
            fallback_count += 1
            if frame.high_resolution_capture_failure is None:
                raise ScanValidationError(
                    f"frames.json[{index}] fallback image must declare a reason"
                )

    if (
        high_resolution_count != session.high_resolution_image_count
        or fallback_count != session.fallback_image_count
    ):
        raise ScanValidationError(
            "Per-frame image_source counts do not match session high-resolution/fallback counts"
        )
    if high_resolution_count + fallback_count != len(metadata.frames):
        raise ScanValidationError("High-resolution/fallback counts do not cover every frame")
    if session.high_resolution_frame_capture_enabled is False and high_resolution_count:
        raise ScanValidationError(
            "High-resolution images cannot be declared when capture support is disabled"
        )


def _validate_capture_masks(
    scan_dir: Path,
    metadata: ScanMetadata,
    *,
    has_mask_authoring: bool,
) -> int:
    scope = metadata.reconstruction_scope
    masks_root = scan_dir / "masks"
    if scope is None:
        if masks_root.exists() or masks_root.is_symlink():
            if not has_mask_authoring:
                raise ScanValidationError("masks directory requires reconstruction_scope metadata")
            _validate_proposed_masks(
                scan_dir,
                masks_root,
                metadata.frames,
                allow_capture=False,
            )
        return 0

    capture_dir = masks_root / "capture"
    _validate_owned_directory(scan_dir, masks_root, label="masks")
    _validate_owned_directory(scan_dir, capture_dir, label="masks/capture")
    root_entries = list(masks_root.iterdir())
    allowed = {capture_dir.resolve()}
    proposed_dir = masks_root / "proposed"
    if proposed_dir.exists() or proposed_dir.is_symlink():
        if not has_mask_authoring:
            raise ScanValidationError("Proposed masks require mask-authoring metadata")
        _validate_proposed_masks(
            scan_dir,
            masks_root,
            metadata.frames,
            allow_capture=True,
        )
        allowed.add(proposed_dir.resolve())
        review_dir = masks_root / "review"
        if review_dir.exists() or review_dir.is_symlink():
            allowed.add(review_dir.resolve())
    if {entry.resolve() for entry in root_entries} != allowed:
        raise ScanValidationError("masks contains an unsupported directory or file")
    mask_files = _validated_flat_files(capture_dir, label="masks/capture")
    expected_names = {Path(frame.image).name + ".png" for frame in metadata.frames}
    actual_names = {path.name for path in mask_files}
    if len(mask_files) != scope.mask_count:
        raise ScanValidationError(
            f"reconstruction_scope mask_count ({scope.mask_count}) does not match mask files ({len(mask_files)})"
        )
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        extra = sorted(actual_names - expected_names)
        raise ScanValidationError(f"Capture mask association mismatch; missing={missing}, extra={extra}")
    frames_by_mask = {Path(frame.image).name + ".png": frame for frame in metadata.frames}
    try:
        for mask in mask_files:
            validate_capture_mask_png(mask, frames_by_mask[mask.name].resolution)
    except MaskValidationError as error:
        raise ScanValidationError(str(error)) from error
    return len(mask_files)


def _validate_proposed_masks(
    scan_dir: Path,
    masks_root: Path,
    frames: tuple[FrameMetadata, ...],
    *,
    allow_capture: bool,
) -> None:
    _validate_owned_directory(scan_dir, masks_root, label="masks")
    proposed_dir = masks_root / "proposed"
    review_dir = masks_root / "review"
    root_entries = list(masks_root.iterdir())
    allowed_names = {"proposed", "review"}
    if allow_capture:
        allowed_names.add("capture")
    if any(entry.name not in allowed_names for entry in root_entries):
        raise ScanValidationError("masks contains an unsupported directory or file")
    _validate_owned_directory(scan_dir, proposed_dir, label="masks/proposed")
    proposed_files = _validated_flat_files(proposed_dir, label="masks/proposed")
    expected_names = {Path(frame.image).name + ".png" for frame in frames}
    actual_names = {path.name for path in proposed_files}
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        extra = sorted(actual_names - expected_names)
        raise ScanValidationError(
            f"Proposed mask association mismatch; missing={missing}, extra={extra}"
        )
    frames_by_mask = {Path(frame.image).name + ".png": frame for frame in frames}
    try:
        for mask in proposed_files:
            validate_capture_mask_png(mask, frames_by_mask[mask.name].resolution)
    except MaskValidationError as error:
        raise ScanValidationError(str(error)) from error
    if review_dir.exists() or review_dir.is_symlink():
        _validate_review_previews(scan_dir, review_dir, frames)


def _validate_review_previews(
    scan_dir: Path,
    review_dir: Path,
    frames: tuple[FrameMetadata, ...],
) -> None:
    _validate_owned_directory(scan_dir, review_dir, label="masks/review")
    review_files = _validated_flat_files(review_dir, label="masks/review")
    expected_names = {
        Path(frames[index].image).name + ".png"
        for index in representative_frame_indices(len(frames))
    }
    actual_names = {path.name for path in review_files}
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        extra = sorted(actual_names - expected_names)
        raise ScanValidationError(
            f"Mask review association mismatch; missing={missing}, extra={extra}"
        )
    try:
        for path in review_files:
            with Image.open(path) as image:
                width, height = image.size
                if (
                    image.format != "PNG"
                    or width < 1
                    or height < 1
                    or width > 1600
                    or height > 1600
                ):
                    raise ScanValidationError(
                        f"Mask review image format or dimensions are unsafe: {path.name}"
                    )
                image.load()
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise ScanValidationError("Unable to decode mask review PNG") from error


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
