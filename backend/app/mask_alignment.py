"""Durable checkpoint for mask review before foreground-only alignment."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from app.mask_profiles import MaskProfileName
from app.openmvs_runner import OpenMVSScopeMode


class MaskAlignmentCheckpointError(ValueError):
    """Raised when a pre-alignment mask checkpoint is malformed or unsafe."""


@dataclass(frozen=True)
class MaskAlignmentCheckpoint:
    run_dense: bool
    run_openmvs: bool
    scope_mode: OpenMVSScopeMode
    use_masks: bool
    review_scope: bool
    mask_profile: MaskProfileName


def publish_mask_alignment_checkpoint(
    scan_root: Path,
    *,
    run_dense: bool,
    run_openmvs: bool,
    scope_mode: OpenMVSScopeMode,
    use_masks: bool,
    review_scope: bool,
    mask_profile: MaskProfileName,
) -> Path:
    """Persist enough intent to begin alignment only after mask approval."""
    if mask_profile != "object_foreground":
        raise MaskAlignmentCheckpointError(
            "Pre-alignment mask review is only valid for object_foreground jobs"
        )
    if not review_scope:
        raise MaskAlignmentCheckpointError(
            "Pre-alignment mask review requires sparse scope review"
        )
    path = scan_root.resolve() / "metadata" / "mask_alignment_checkpoint.json"
    payload = {
        "schema_version": "1.0",
        "state": "awaiting_masks",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "continuation": {
            "run_dense": run_dense,
            "run_openmvs": run_openmvs,
            "scope_mode": scope_mode,
            "use_masks": use_masks,
            "review_scope": review_scope,
            "mask_profile": mask_profile,
        },
    }
    _write_json_atomic(path, payload)
    return path


def load_mask_alignment_checkpoint(scan_root: Path) -> MaskAlignmentCheckpoint:
    """Strictly validate a saved pre-alignment continuation contract."""
    path = scan_root.resolve() / "metadata" / "mask_alignment_checkpoint.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
        raise MaskAlignmentCheckpointError(
            f"Mask-alignment checkpoint is missing or unsafe: {path}"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MaskAlignmentCheckpointError(
            f"Mask-alignment checkpoint is invalid: {path}"
        ) from error
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version", "state", "created_at", "continuation"
    }:
        raise MaskAlignmentCheckpointError("Mask-alignment checkpoint fields are invalid")
    if payload["schema_version"] != "1.0" or payload["state"] != "awaiting_masks":
        raise MaskAlignmentCheckpointError("Mask-alignment checkpoint identity is invalid")
    created_at = payload["created_at"]
    if not isinstance(created_at, str):
        raise MaskAlignmentCheckpointError("Mask-alignment timestamp is invalid")
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise MaskAlignmentCheckpointError("Mask-alignment timestamp is invalid") from error
    if created.tzinfo is None or created.utcoffset() is None:
        raise MaskAlignmentCheckpointError("Mask-alignment timestamp requires a timezone")

    continuation = payload["continuation"]
    expected = {
        "run_dense", "run_openmvs", "scope_mode", "use_masks", "review_scope",
        "mask_profile",
    }
    if not isinstance(continuation, dict) or set(continuation) != expected:
        raise MaskAlignmentCheckpointError("Mask-alignment continuation is invalid")
    flags = ("run_dense", "run_openmvs", "use_masks", "review_scope")
    if any(type(continuation[name]) is not bool for name in flags):
        raise MaskAlignmentCheckpointError("Mask-alignment continuation flags are invalid")
    if continuation["scope_mode"] not in {"auto_roi", "unbounded"}:
        raise MaskAlignmentCheckpointError("Mask-alignment scope mode is invalid")
    if continuation["mask_profile"] != "object_foreground":
        raise MaskAlignmentCheckpointError("Mask-alignment profile is invalid")
    if not continuation["review_scope"]:
        raise MaskAlignmentCheckpointError("Mask-alignment scope review is required")
    return MaskAlignmentCheckpoint(
        run_dense=continuation["run_dense"],
        run_openmvs=continuation["run_openmvs"],
        scope_mode=continuation["scope_mode"],
        use_masks=continuation["use_masks"],
        review_scope=continuation["review_scope"],
        mask_profile=continuation["mask_profile"],
    )


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, indent=2, sort_keys=True, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
