"""Publish the sparse artifacts needed for post-alignment scope review."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from app.openmvs_runner import OpenMVSScopeMode


class SparseReviewError(ValueError):
    """Raised when a sparse model cannot produce a safe review checkpoint."""


ModelExporter = Callable[[Path, Path], None]


def publish_sparse_review_checkpoint(
    scan_root: Path,
    *,
    run_dense: bool,
    run_openmvs: bool,
    scope_mode: OpenMVSScopeMode,
    use_masks: bool,
    colmap_executable: str = "colmap",
    model_exporter: ModelExporter | None = None,
) -> dict[str, Path]:
    """Publish camera poses and a resumable sparse-review checkpoint."""
    scan_root = scan_root.resolve()
    sparse_model = scan_root / "sparse" / "0"
    sparse_points = scan_root / "sparse" / "sparse_points.ply"
    if not sparse_model.is_dir() or sparse_model.is_symlink():
        raise SparseReviewError(f"Sparse COLMAP model is missing or unsafe: {sparse_model}")
    if not sparse_points.is_file() or sparse_points.is_symlink():
        raise SparseReviewError(f"Sparse point-cloud preview is missing or unsafe: {sparse_points}")

    exporter = model_exporter or (
        lambda source, output: _export_colmap_model(source, output, colmap_executable)
    )
    cameras_path = scan_root / "sparse" / "cameras_preview.json"
    with tempfile.TemporaryDirectory(dir=scan_root / "sparse", prefix=".review-model.") as tmp:
        text_model = Path(tmp)
        exporter(sparse_model, text_model)
        camera_payload = _camera_preview_payload(text_model / "images.txt")
    _write_json_atomic(cameras_path, camera_payload)

    checkpoint_path = scan_root / "metadata" / "reconstruction_checkpoint.json"
    checkpoint_payload = {
        "schema_version": "1.0",
        "state": "awaiting_scope",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "coordinate_system": "colmap_reconstruction",
        "sparse_model": "sparse/0",
        "sparse_point_cloud": "sparse/sparse_points.ply",
        "camera_preview": "sparse/cameras_preview.json",
        "continuation": {
            "run_dense": run_dense,
            "run_openmvs": run_openmvs,
            "scope_mode": scope_mode,
            "use_masks": use_masks,
        },
    }
    _write_json_atomic(checkpoint_path, checkpoint_payload)
    return {
        "sparse_point_cloud": sparse_points,
        "sparse_camera_preview": cameras_path,
        "scope_review_checkpoint": checkpoint_path,
    }


def _camera_preview_payload(images_path: Path) -> dict[str, Any]:
    cameras = _parse_colmap_image_poses(images_path)
    return {
        "schema_version": "1.0",
        "coordinate_system": "colmap_reconstruction",
        "camera_count": len(cameras),
        "cameras": cameras,
    }


def _parse_colmap_image_poses(path: Path) -> list[dict[str, Any]]:
    if not path.is_file() or path.is_symlink():
        raise SparseReviewError(f"COLMAP image poses are missing or unsafe: {path}")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise SparseReviewError(f"Unable to read COLMAP image poses: {path}") from error

    cameras: list[dict[str, Any]] = []
    image_ids: set[int] = set()
    image_names: set[str] = set()
    expect_image_record = True
    for line_number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if line.startswith("#"):
            continue
        if not expect_image_record:
            _validate_points2d_record(line, path, line_number)
            expect_image_record = True
            continue
        if not line:
            continue

        parts = line.split(maxsplit=9)
        if len(parts) != 10:
            raise SparseReviewError(f"Invalid image pose at {path}:{line_number}")
        try:
            image_id = int(parts[0])
            quaternion = tuple(float(value) for value in parts[1:5])
            translation = tuple(float(value) for value in parts[5:8])
            camera_id = int(parts[8])
        except ValueError as error:
            raise SparseReviewError(f"Invalid image pose value at {path}:{line_number}") from error
        image_name = parts[9]
        if image_id < 1 or camera_id < 1 or not image_name:
            raise SparseReviewError(f"Invalid image pose identity at {path}:{line_number}")
        if image_id in image_ids or image_name in image_names:
            raise SparseReviewError(f"Duplicate image pose at {path}:{line_number}")
        if not all(math.isfinite(value) for value in (*quaternion, *translation)):
            raise SparseReviewError(f"Image pose values must be finite at {path}:{line_number}")
        norm = math.sqrt(sum(value * value for value in quaternion))
        if abs(norm - 1.0) > 1e-4:
            raise SparseReviewError(f"Image pose quaternion must be normalized at {path}:{line_number}")

        center = _camera_center(quaternion, translation)
        cameras.append(
            {
                "image_id": image_id,
                "image_name": image_name,
                "camera_id": camera_id,
                "rotation_world_to_camera_wxyz": list(quaternion),
                "translation_world_to_camera": list(translation),
                "center": list(center),
            }
        )
        image_ids.add(image_id)
        image_names.add(image_name)
        expect_image_record = False

    if not expect_image_record:
        raise SparseReviewError(f"Image pose has no POINTS2D record in {path}")
    if not cameras:
        raise SparseReviewError(f"No registered camera poses found in {path}")
    cameras.sort(key=lambda item: int(item["image_id"]))
    return cameras


def _validate_points2d_record(line: str, path: Path, line_number: int) -> None:
    values = line.split()
    if len(values) % 3 != 0:
        raise SparseReviewError(f"Invalid POINTS2D record at {path}:{line_number}")
    try:
        for index in range(0, len(values), 3):
            x = float(values[index])
            y = float(values[index + 1])
            int(values[index + 2])
            if not math.isfinite(x) or not math.isfinite(y):
                raise ValueError
    except ValueError as error:
        raise SparseReviewError(f"Invalid POINTS2D value at {path}:{line_number}") from error


def _camera_center(
    quaternion_wxyz: tuple[float, ...],
    translation: tuple[float, ...],
) -> tuple[float, float, float]:
    """Convert COLMAP's world-to-camera pose into a world-space camera center."""
    w, x, y, z = quaternion_wxyz
    tx, ty, tz = translation
    r00 = 1 - 2 * (y * y + z * z)
    r01 = 2 * (x * y - z * w)
    r02 = 2 * (x * z + y * w)
    r10 = 2 * (x * y + z * w)
    r11 = 1 - 2 * (x * x + z * z)
    r12 = 2 * (y * z - x * w)
    r20 = 2 * (x * z - y * w)
    r21 = 2 * (y * z + x * w)
    r22 = 1 - 2 * (x * x + y * y)
    return (
        -(r00 * tx + r10 * ty + r20 * tz),
        -(r01 * tx + r11 * ty + r21 * tz),
        -(r02 * tx + r12 * ty + r22 * tz),
    )


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _export_colmap_model(source: Path, output: Path, executable: str) -> None:
    subprocess.run(
        [
            executable,
            "model_converter",
            "--input_path",
            str(source),
            "--output_path",
            str(output),
            "--output_type",
            "TXT",
        ],
        check=True,
    )
