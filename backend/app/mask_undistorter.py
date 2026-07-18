"""COLMAP camera-model transforms for lossless binary mask undistortion."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import os
import tempfile

import numpy as np


class MaskUndistortionError(ValueError):
    """Raised when camera metadata or mask geometry is unsupported."""


@dataclass(frozen=True)
class ColmapCamera:
    camera_id: int
    model: str
    width: int
    height: int
    params: tuple[float, ...]


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
        Image.fromarray(transformed, mode="L").save(temporary_path, format="PNG", optimize=False)
        with temporary_path.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary_path, output_path)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    return output_path
