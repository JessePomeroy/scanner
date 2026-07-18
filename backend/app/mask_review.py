"""Review-state validation and atomic promotion of proposed capture masks."""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
from typing import Callable

from app.mask_authoring import load_mask_authoring_plan
from app.mask_processor import MaskValidationError, validate_capture_mask_png
from app.scan_metadata import load_scan_metadata


class MaskReviewError(ValueError):
    """Raised when mask review state is missing, stale, or unsafe."""


class MaskReviewBlockedError(MaskReviewError):
    """Raised when automated quality checks require correction."""


Clock = Callable[[], datetime]
_MAX_REPORT_BYTES = 16 * 1024 * 1024


def load_mask_review(scan_root: Path) -> dict[str, object]:
    """Load a bounded report and verify the fields needed by review clients."""
    report_path = scan_root.resolve() / "metadata" / "mask_generation.json"
    if (
        report_path.is_symlink()
        or not report_path.is_file()
        or report_path.stat().st_size > _MAX_REPORT_BYTES
    ):
        raise MaskReviewError("Mask review report is missing or unsafe")
    try:
        payload = json.loads(
            report_path.read_text(encoding="utf-8"),
            parse_constant=lambda value: _reject_constant(value),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MaskReviewError("Mask review report is invalid") from error
    if not isinstance(payload, dict):
        raise MaskReviewError("Mask review report must be a JSON object")
    if payload.get("schema_version") != "1.0":
        raise MaskReviewError("Mask review schema_version must be '1.0'")
    if payload.get("state") not in {
        "awaiting_review", "needs_correction", "approved", "rejected"
    }:
        raise MaskReviewError("Mask review state is invalid")
    if not isinstance(payload.get("frames"), list):
        raise MaskReviewError("Mask review frames must be an array")
    frame_count = payload.get("frame_count")
    if (
        isinstance(frame_count, bool)
        or not isinstance(frame_count, int)
        or frame_count < 1
        or frame_count != len(payload["frames"])
    ):
        raise MaskReviewError("Mask review frame count is invalid")
    quality = payload.get("quality")
    if not isinstance(quality, dict):
        raise MaskReviewError("Mask review quality summary is invalid")
    count = quality.get("blocking_issue_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise MaskReviewError("Mask review blocking issue count is invalid")
    issues = quality.get("blocking_issues")
    if not isinstance(issues, list) or len(issues) != count:
        raise MaskReviewError("Mask review blocking issue list is invalid")
    return payload


def approve_mask_review(
    scan_root: Path,
    *,
    clock: Clock | None = None,
) -> dict[str, object]:
    """Promote one exact, reviewed proposal set into active capture masks."""
    scan_root = scan_root.resolve()
    metadata_dir = scan_root / "metadata"
    masks_root = scan_root / "masks"
    lock_path = metadata_dir / ".mask_generation.lock"
    if masks_root.is_symlink() or lock_path.is_symlink():
        raise MaskReviewError("Mask review workspace is unsafe")
    lock_fd = os.open(
        lock_path,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    staging: Path | None = None
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        payload = load_mask_review(scan_root)
        state = payload["state"]
        if state == "needs_correction":
            raise MaskReviewBlockedError(
                "Mask proposals have blocking quality issues and cannot be approved"
            )
        if state != "awaiting_review":
            raise MaskReviewError(f"Mask proposals cannot be approved from state {state!r}")

        metadata = load_scan_metadata(metadata_dir)
        plan = load_mask_authoring_plan(metadata_dir, metadata.frames)
        source_revision = payload.get("source_authoring_revision")
        if (
            plan is None
            or isinstance(source_revision, bool)
            or not isinstance(source_revision, int)
            or source_revision != plan.revision
        ):
            raise MaskReviewError("Mask proposals do not match the current authoring revision")
        if metadata.reconstruction_scope is not None:
            raise MaskReviewError("Capture masks are already active")
        quality = payload["quality"]
        assert isinstance(quality, dict)
        if quality["blocking_issue_count"] != 0:
            raise MaskReviewBlockedError("Mask proposals have blocking quality issues")

        proposed = masks_root / "proposed"
        capture = masks_root / "capture"
        if proposed.is_symlink() or not proposed.is_dir() or capture.exists() or capture.is_symlink():
            raise MaskReviewError("Mask proposal or capture directory is unsafe")
        expected = {Path(frame.image).name + ".png": frame for frame in metadata.frames}
        actual_files = list(proposed.iterdir())
        if any(path.is_symlink() or not path.is_file() for path in actual_files):
            raise MaskReviewError("Mask proposal directory contains an unsafe entry")
        if {path.name for path in actual_files} != set(expected):
            raise MaskReviewError("Mask proposal set is incomplete or has extra files")

        report_frames = payload["frames"]
        assert isinstance(report_frames, list)
        associations: set[tuple[int, str, str]] = set()
        for item in report_frames:
            if not isinstance(item, dict):
                raise MaskReviewError("Mask review frame entry is invalid")
            frame_id = item.get("frame_id")
            image = item.get("image")
            mask = item.get("mask")
            if (
                isinstance(frame_id, bool)
                or not isinstance(frame_id, int)
                or not isinstance(image, str)
                or not isinstance(mask, str)
            ):
                raise MaskReviewError("Mask review frame association is invalid")
            associations.add((frame_id, image, mask))
        expected_associations = {
            (frame.id, frame.image, f"masks/proposed/{name}")
            for name, frame in expected.items()
        }
        if len(report_frames) != len(expected) or associations != expected_associations:
            raise MaskReviewError("Mask review frame associations are incomplete or stale")

        staging = Path(tempfile.mkdtemp(dir=masks_root, prefix=".capture."))
        try:
            for source in actual_files:
                copied = staging / source.name
                _copy_regular_no_follow(source, copied)
                validate_capture_mask_png(copied, expected[source.name].resolution)
        except (MaskValidationError, OSError) as error:
            raise MaskReviewError("Mask proposal set failed promotion validation") from error

        manifest_path = metadata_dir / "manifest.json"
        manifest = _load_json_object(manifest_path)
        original_manifest = manifest_path.read_bytes() if manifest_path.exists() else None
        manifest["reconstruction_scope"] = {
            "schema_version": "1.0",
            "mode": "image_masks",
            "mask_space": "capture_image",
            "mask_convention": "white_keep_black_exclude",
            "mask_count": len(expected),
        }
        approved = dict(payload)
        approved["state"] = "approved"
        approved["decision"] = {
            "decision": "approve",
            "decided_at": _timestamp(clock),
            "promoted_mask_count": len(expected),
        }
        report_path = metadata_dir / "mask_generation.json"
        try:
            os.replace(staging, capture)
            staging = None
            _write_json_atomic(manifest_path, manifest)
            _write_json_atomic(report_path, approved)
        except BaseException:
            if capture.exists():
                shutil.rmtree(capture, ignore_errors=True)
            _restore_file(manifest_path, original_manifest)
            raise
        return approved
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def reject_mask_review(
    scan_root: Path,
    *,
    clock: Clock | None = None,
) -> dict[str, object]:
    """Record rejection without activating or deleting proposal evidence."""
    scan_root = scan_root.resolve()
    lock_path = scan_root / "metadata" / ".mask_generation.lock"
    if lock_path.is_symlink():
        raise MaskReviewError("Mask review workspace is unsafe")
    lock_fd = os.open(
        lock_path,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        payload = load_mask_review(scan_root)
        if payload["state"] not in {"awaiting_review", "needs_correction"}:
            raise MaskReviewError(
                f"Mask proposals cannot be rejected from state {payload['state']!r}"
            )
        rejected = dict(payload)
        rejected["state"] = "rejected"
        rejected["decision"] = {
            "decision": "reject",
            "decided_at": _timestamp(clock),
        }
        _write_json_atomic(scan_root / "metadata" / "mask_generation.json", rejected)
        return rejected
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _timestamp(clock: Clock | None) -> str:
    value = (clock or (lambda: datetime.now(timezone.utc)))()
    if value.tzinfo is None or value.utcoffset() is None:
        raise MaskReviewError("Mask review clock must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _load_json_object(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise MaskReviewError("Scan manifest is unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MaskReviewError("Scan manifest is invalid") from error
    if not isinstance(value, dict):
        raise MaskReviewError("Scan manifest must be a JSON object")
    return value


def _copy_regular_no_follow(source: Path, destination: Path) -> None:
    descriptor = os.open(
        source,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        source_stat = os.fstat(descriptor)
        if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_nlink != 1:
            raise MaskReviewError("Mask proposal file is not an owned regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as source_file:
            with destination.open("xb") as destination_file:
                shutil.copyfileobj(source_file, destination_file, length=1024 * 1024)
                destination_file.flush()
                os.fsync(destination_file.fileno())
    finally:
        os.close(descriptor)


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(payload, temporary, indent=2, sort_keys=True, allow_nan=False)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _restore_file(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
        return
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".restore",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _reject_constant(value: str) -> None:
    raise MaskReviewError(f"Mask review report contains non-finite number {value}")
