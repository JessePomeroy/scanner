"""One-box object selection supplied after upload instead of inside the package.

The phone ZIP is never edited. Everything here changes only the job's extracted
working copy, and earlier mask choices are moved aside rather than deleted.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Literal

from app.mask_authoring import MASK_AUTHORING_FILENAME, load_mask_authoring_plan
from app.retro_style import TRIANGLE_BUDGETS
from app.scan_metadata import FrameMetadata, load_scan_metadata


ObjectPreset = Literal["preview", "detail"]
ExportStyle = Literal["standard", "retro"]
Box = tuple[float, float, float, float]

CHECKPOINT_FILENAME = "object_box_checkpoint.json"
SUPERSEDED_DIRNAME = "superseded"
MINIMUM_BOX_SIDE = 0.02
_REQUEST_FIELDS = {"frame_id", "box", "preset", "export_style", "retro_triangles"}


class ObjectBoxError(ValueError):
    """Raised when a box request or its paused workspace is malformed or unsafe."""


@dataclass(frozen=True)
class ObjectBoxOptions:
    """Reconstruction choices that travel with a box, with safe defaults."""

    preset: ObjectPreset = "preview"
    export_style: ExportStyle = "standard"
    retro_triangles: int = 500

    def __post_init__(self) -> None:
        if self.preset not in ("preview", "detail"):
            raise ObjectBoxError("preset must be 'preview' or 'detail'")
        if self.export_style not in ("standard", "retro"):
            raise ObjectBoxError("export_style must be 'standard' or 'retro'")
        if type(self.retro_triangles) is not int or self.retro_triangles not in TRIANGLE_BUDGETS:
            raise ObjectBoxError("retro_triangles must be 500, 1000 or 2000")


@dataclass(frozen=True)
class ObjectBoxRequest:
    frame_id: int
    box: Box
    options: ObjectBoxOptions


def parse_box(value: object) -> Box:
    """Accept [x0, y0, x1, y1] in normalized saved-photo coordinates."""
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ObjectBoxError("box must be [x0, y0, x1, y1]")
    numbers: list[float] = []
    for item in value:
        # The range test also rejects NaN, infinities and oversized integers.
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not 0 <= item <= 1:
            raise ObjectBoxError("box values must be finite numbers from 0 through 1")
        numbers.append(float(item))
    x0, y0, x1, y1 = numbers
    if x1 - x0 < MINIMUM_BOX_SIDE or y1 - y0 < MINIMUM_BOX_SIDE:
        raise ObjectBoxError("box is too small; drag it around the whole object")
    return x0, y0, x1, y1


def parse_object_box_request(payload: object, defaults: ObjectBoxOptions) -> ObjectBoxRequest:
    """Strictly parse a client request; omitted choices fall back to the upload's."""
    if not isinstance(payload, dict):
        raise ObjectBoxError("request must be a JSON object")
    unknown = sorted(set(payload) - _REQUEST_FIELDS)
    if unknown:
        raise ObjectBoxError(f"request has unsupported fields: {unknown}")
    frame_id = payload.get("frame_id")
    if isinstance(frame_id, bool) or not isinstance(frame_id, int) or frame_id < 0:
        raise ObjectBoxError("frame_id must be a non-negative integer")
    choices = asdict(defaults)
    for name in ("preset", "export_style", "retro_triangles"):
        if payload.get(name) is not None:
            choices[name] = payload[name]
    if choices["export_style"] != "retro":
        if payload.get("retro_triangles") not in (None, 500):
            raise ObjectBoxError("retro_triangles requires export_style 'retro'")
        choices["retro_triangles"] = 500
    return ObjectBoxRequest(frame_id, parse_box(payload.get("box")), ObjectBoxOptions(**choices))


def needs_object_box(scan_root: Path) -> bool:
    """True unless the package already carries an explicit single-box selection."""
    metadata_dir = scan_root / "metadata"
    plan = load_mask_authoring_plan(metadata_dir, load_scan_metadata(metadata_dir).frames)
    return plan is None or plan.authoring_mode != "single_box"


def frame_for_box(scan_root: Path, frame_id: int) -> FrameMetadata:
    for frame in load_scan_metadata(scan_root / "metadata").frames:
        if frame.id == frame_id:
            return frame
    raise ObjectBoxError(f"frame_id {frame_id} is not a photo in this scan")


CheckpointState = Literal["awaiting_box", "queued"]


def publish_object_box_checkpoint(
    scan_root: Path, options: ObjectBoxOptions, state: CheckpointState = "awaiting_box",
) -> Path:
    """Persist the job's choices so a paused or queued job survives a restart.

    ``awaiting_box`` waits for a person. ``queued`` has its box and waits only
    for the computer; it is removed the moment the job is admitted, so its
    presence proves no native work has started.
    """
    path = scan_root.resolve() / "metadata" / CHECKPOINT_FILENAME
    _write_json_atomic(path, {"schema_version": "1.0", "state": state, "options": asdict(options)})
    return path


def load_object_box_checkpoint(
    scan_root: Path, state: CheckpointState = "awaiting_box",
) -> ObjectBoxOptions:
    path = scan_root.resolve() / "metadata" / CHECKPOINT_FILENAME
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
        raise ObjectBoxError(f"Object-box checkpoint is missing or unsafe: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ObjectBoxError(f"Object-box checkpoint is invalid: {path}") from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "state", "options"}
        or payload["schema_version"] != "1.0"
        or payload["state"] != state
        or not isinstance(payload["options"], dict)
        or set(payload["options"]) != {"preset", "export_style", "retro_triangles"}
    ):
        raise ObjectBoxError("Object-box checkpoint fields are invalid")
    return ObjectBoxOptions(**payload["options"])


def clear_object_box_checkpoint(scan_root: Path) -> None:
    (scan_root.resolve() / "metadata" / CHECKPOINT_FILENAME).unlink(missing_ok=True)


def check_object_box_workspace(scan_root: Path) -> None:
    """Refuse, before anything changes, a workspace a box cannot be applied to."""
    scan_root = scan_root.resolve()
    superseded = scan_root / SUPERSEDED_DIRNAME
    if superseded.exists() or superseded.is_symlink():
        raise ObjectBoxError("This scan already has an object box applied")
    metadata_dir = scan_root / "metadata"
    for path in (metadata_dir, scan_root / "masks", metadata_dir / MASK_AUTHORING_FILENAME,
                 metadata_dir / "manifest.json"):
        if path.is_symlink():
            raise ObjectBoxError(f"Scan workspace is unsafe: {path.name}")
    _load_manifest(metadata_dir / "manifest.json")


def apply_object_box(scan_root: Path, frame_id: int, box: Box) -> dict[str, object]:
    """Make this box the job's only mask choice.

    A capture-time outline, an older mask draft, or generated proposals would
    conflict with single-box tracking. They are moved to ``superseded/`` inside
    this working copy; the uploaded ZIP still holds the originals.

    Every new file is written first. If that fails the workspace is left as it
    was and ``ObjectBoxError`` is raised, so the box can be sent again. Only
    renames, which need no free space, follow.
    """
    scan_root = scan_root.resolve()
    metadata_dir = scan_root / "metadata"
    frame = frame_for_box(scan_root, frame_id)
    x0, y0, x1, y1 = box = parse_box(list(box))
    check_object_box_workspace(scan_root)
    superseded = scan_root / SUPERSEDED_DIRNAME
    masks = scan_root / "masks"
    plan_path = metadata_dir / MASK_AUTHORING_FILENAME
    manifest_path = metadata_dir / "manifest.json"
    manifest = _load_manifest(manifest_path)
    scope = manifest.pop("reconstruction_scope", None)
    new_plan = metadata_dir / f".{MASK_AUTHORING_FILENAME}.box"
    new_manifest = metadata_dir / ".manifest.json.box"
    try:
        superseded.mkdir()
        _write_json_atomic(new_plan, {
            "schema_version": "1.1",
            "authoring_mode": "single_box",
            "coordinate_space": "normalized_capture_image",
            "mask_convention": "white_keep_black_exclude",
            "revision": 1,
            "representative_frames": [{
                "frame_id": frame.id,
                "image": frame.image,
                "regions": [{"operation": "keep", "points": [
                    {"x": x0, "y": y0}, {"x": x1, "y": y0}, {"x": x1, "y": y1}, {"x": x0, "y": y1},
                ]}],
            }],
        })
        if scope is not None:
            _write_json_atomic(superseded / "reconstruction_scope.json", {"reconstruction_scope": scope})
            _write_json_atomic(new_manifest, manifest)
    except Exception as error:
        new_plan.unlink(missing_ok=True)
        new_manifest.unlink(missing_ok=True)
        (superseded / "reconstruction_scope.json").unlink(missing_ok=True)
        if superseded.is_dir():
            superseded.rmdir()
        raise ObjectBoxError("The box could not be saved; the scan is unchanged") from error
    moved: list[str] = []
    if masks.exists():
        os.replace(masks, superseded / "masks")
        moved.append("masks")
    if plan_path.exists():
        os.replace(plan_path, superseded / MASK_AUTHORING_FILENAME)
        moved.append(f"metadata/{MASK_AUTHORING_FILENAME}")
    if scope is not None:
        os.replace(new_manifest, manifest_path)
        moved.append("metadata/manifest.json reconstruction_scope")
    os.replace(new_plan, plan_path)
    if not moved:
        superseded.rmdir()
    return {"frame_id": frame.id, "image": frame.image, "box": list(box), "superseded": moved}


def _load_manifest(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ObjectBoxError("Scan manifest is invalid") from error
    if not isinstance(manifest, dict):
        raise ObjectBoxError("Scan manifest must be a JSON object")
    return manifest


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
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
