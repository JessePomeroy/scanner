"""COLMAP camera-model transforms for lossless binary mask undistortion."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import math
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
from typing import Callable

import numpy as np

from app.mask_processor import MaskValidationResult, validate_openmvs_masks


class MaskUndistortionError(ValueError):
    """Raised when camera metadata or mask geometry is unsupported."""


@dataclass(frozen=True)
class ColmapCamera:
    camera_id: int
    model: str
    width: int
    height: int
    params: tuple[float, ...]


@dataclass(frozen=True)
class MaskCameraAssociation:
    image_name: str
    source: ColmapCamera
    target: ColmapCamera


def parse_colmap_cameras(path: Path) -> dict[int, ColmapCamera]:
    """Parse COLMAP cameras.txt without reading any reconstruction payloads."""
    cameras: dict[int, ColmapCamera] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise MaskUndistortionError(f"Unable to read COLMAP cameras: {path}") from error
    for line_number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 5:
            raise MaskUndistortionError(f"Invalid camera record at {path}:{line_number}")
        try:
            camera = ColmapCamera(
                camera_id=int(parts[0]),
                model=parts[1],
                width=int(parts[2]),
                height=int(parts[3]),
                params=tuple(float(value) for value in parts[4:]),
            )
        except ValueError as error:
            raise MaskUndistortionError(f"Invalid camera value at {path}:{line_number}") from error
        if camera.camera_id in cameras or camera.width < 1 or camera.height < 1:
            raise MaskUndistortionError(f"Invalid or duplicate camera id at {path}:{line_number}")
        if not camera.params or not all(math.isfinite(value) for value in camera.params):
            raise MaskUndistortionError(f"Camera parameters must be finite at {path}:{line_number}")
        cameras[camera.camera_id] = camera
    if not cameras:
        raise MaskUndistortionError(f"No cameras found in {path}")
    return cameras


def parse_colmap_image_cameras(path: Path) -> dict[str, int]:
    """Parse image-name to camera-id associations from COLMAP images.txt."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise MaskUndistortionError(f"Unable to read COLMAP images: {path}") from error
    records: dict[str, int] = {}
    expect_image_record = True
    for line_number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if line.startswith("#"):
            continue
        if not expect_image_record:
            point_values = line.split()
            if len(point_values) % 3 != 0:
                raise MaskUndistortionError(f"Invalid POINTS2D record at {path}:{line_number}")
            try:
                for index in range(0, len(point_values), 3):
                    float(point_values[index])
                    float(point_values[index + 1])
                    int(point_values[index + 2])
            except ValueError as error:
                raise MaskUndistortionError(f"Invalid POINTS2D value at {path}:{line_number}") from error
            expect_image_record = True
            continue
        if not line:
            continue
        parts = line.split(maxsplit=9)
        if len(parts) != 10:
            raise MaskUndistortionError(f"Invalid image record at {path}:{line_number}")
        try:
            int(parts[0])
            camera_id = int(parts[8])
        except ValueError as error:
            raise MaskUndistortionError(f"Invalid image value at {path}:{line_number}") from error
        name = parts[9]
        if not name or name in records:
            raise MaskUndistortionError(f"Empty or duplicate image name at {path}:{line_number}")
        records[name] = camera_id
        expect_image_record = False
    if not records:
        raise MaskUndistortionError(f"No images found in {path}")
    if not expect_image_record:
        raise MaskUndistortionError(f"Image record has no POINTS2D line in {path}")
    return records


def associate_mask_cameras(
    source_cameras: dict[int, ColmapCamera],
    source_images: dict[str, int],
    target_cameras: dict[int, ColmapCamera],
    target_images: dict[str, int],
) -> dict[str, MaskCameraAssociation]:
    """Pair original and dense cameras for every identically named image."""
    if source_images.keys() != target_images.keys():
        missing = sorted(source_images.keys() - target_images.keys())
        extra = sorted(target_images.keys() - source_images.keys())
        raise MaskUndistortionError(f"COLMAP image association mismatch; missing={missing}, extra={extra}")
    associations: dict[str, MaskCameraAssociation] = {}
    for name, source_camera_id in source_images.items():
        target_camera_id = target_images[name]
        try:
            source = source_cameras[source_camera_id]
            target = target_cameras[target_camera_id]
        except KeyError as error:
            raise MaskUndistortionError(f"Image {name} references an unknown camera id") from error
        associations[name] = MaskCameraAssociation(name, source, target)
    return associations


def undistort_mask_array(
    mask: np.ndarray,
    source: ColmapCamera,
    target: ColmapCamera,
) -> np.ndarray:
    """Map a capture mask into COLMAP's undistorted PINHOLE image geometry."""
    if mask.ndim != 2 or mask.shape != (source.height, source.width):
        raise MaskUndistortionError(
            f"Mask shape {mask.shape} does not match source camera {(source.height, source.width)}"
        )
    if source.model != "SIMPLE_RADIAL" or len(source.params) != 4:
        raise MaskUndistortionError(f"Unsupported source camera model: {source.model}")
    if target.model != "PINHOLE" or len(target.params) != 4:
        raise MaskUndistortionError(f"Unsupported target camera model: {target.model}")

    source_f, source_cx, source_cy, radial_k = source.params
    target_fx, target_fy, target_cx, target_cy = target.params
    if min(source_f, target_fx, target_fy) <= 0:
        raise MaskUndistortionError("Camera focal lengths must be positive")

    target_y, target_x = np.indices((target.height, target.width), dtype=np.float64)
    normalized_x = (target_x - target_cx) / target_fx
    normalized_y = (target_y - target_cy) / target_fy
    radius_squared = normalized_x * normalized_x + normalized_y * normalized_y
    distortion = 1.0 + radial_k * radius_squared
    source_x = np.floor(source_f * normalized_x * distortion + source_cx + 0.5).astype(np.int64)
    source_y = np.floor(source_f * normalized_y * distortion + source_cy + 0.5).astype(np.int64)

    valid = (
        (source_x >= 0)
        & (source_x < source.width)
        & (source_y >= 0)
        & (source_y < source.height)
    )
    output = np.zeros((target.height, target.width), dtype=np.uint8)
    output[valid] = np.where(mask[source_y[valid], source_x[valid]] > 127, 255, 0)
    return output


def undistort_mask_file(
    input_path: Path,
    output_path: Path,
    source: ColmapCamera,
    target: ColmapCamera,
) -> Path:
    """Decode, transform, and atomically publish a lossless grayscale PNG mask."""
    try:
        from PIL import Image
    except ImportError as error:
        raise RuntimeError("Pillow is required for mask PNG conversion") from error
    if output_path.exists() or output_path.is_symlink():
        raise MaskUndistortionError(f"Refusing to overwrite mask output: {output_path}")
    try:
        with Image.open(input_path) as image:
            image.load()
            mask = np.asarray(image.convert("L"), dtype=np.uint8)
    except (OSError, ValueError) as error:
        raise MaskUndistortionError(f"Unable to decode capture mask: {input_path}") from error

    transformed = undistort_mask_array(mask, source, target)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp.png",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        Image.fromarray(transformed).save(temporary_path, format="PNG", optimize=False)
        with temporary_path.open("rb") as stream:
            os.fsync(stream.fileno())
        try:
            os.link(temporary_path, output_path)
        except FileExistsError as error:
            raise MaskUndistortionError(f"Refusing to overwrite mask output: {output_path}") from error
        temporary_path.unlink()
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    return output_path


ModelExporter = Callable[[Path, Path], None]


def convert_capture_mask_set(
    scan_dir: Path,
    *,
    colmap_executable: str = "colmap",
    model_exporter: ModelExporter | None = None,
) -> MaskValidationResult:
    """Convert every validated capture mask and publish one complete OpenMVS set."""
    scan_dir = scan_dir.resolve()
    dense_dir = scan_dir / "dense"
    output_dir = dense_dir / "masks"
    if output_dir.exists() or output_dir.is_symlink():
        raise MaskUndistortionError(f"Refusing to overwrite mask directory: {output_dir}")
    exporter = model_exporter or (
        lambda source, output: _export_colmap_model(source, output, colmap_executable)
    )
    dense_dir.mkdir(parents=True, exist_ok=True)
    stage_dir: Path | None = None
    lock_fd: int | None = None
    lock_path = dense_dir / ".masks.publish.lock"
    try:
        with tempfile.TemporaryDirectory(dir=dense_dir, prefix=".mask-models.") as temporary:
            temporary_root = Path(temporary)
            source_text = temporary_root / "source"
            target_text = temporary_root / "target"
            source_text.mkdir()
            target_text.mkdir()
            exporter(scan_dir / "sparse" / "0", source_text)
            exporter(dense_dir / "sparse", target_text)
            associations = associate_mask_cameras(
                parse_colmap_cameras(source_text / "cameras.txt"),
                parse_colmap_image_cameras(source_text / "images.txt"),
                parse_colmap_cameras(target_text / "cameras.txt"),
                parse_colmap_image_cameras(target_text / "images.txt"),
            )
            stage_dir = Path(tempfile.mkdtemp(dir=dense_dir, prefix=".masks.stage."))
            for name, association in associations.items():
                capture_mask = scan_dir / "masks" / "capture" / f"{name}.png"
                output_mask = stage_dir / Path(name).with_suffix(".mask.png").name
                undistort_mask_file(capture_mask, output_mask, association.source, association.target)
            result = validate_openmvs_masks(stage_dir, dense_dir / "images")

        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise MaskUndistortionError("Another mask publication is already active") from error
        if output_dir.exists() or output_dir.is_symlink():
            raise MaskUndistortionError(f"Refusing to overwrite mask directory: {output_dir}")
        os.rename(stage_dir, output_dir)
        stage_dir = None
        return MaskValidationResult(output_dir, result.image_count, result.mask_count)
    finally:
        if lock_fd is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        if stage_dir is not None:
            shutil.rmtree(stage_dir, ignore_errors=True)


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
