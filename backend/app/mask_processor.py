"""Validation for OpenMVS-ready masks in the undistorted image space."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import struct
import tempfile

from PIL import Image


class MaskValidationError(ValueError):
    """Raised when an OpenMVS mask set is incomplete or malformed."""


@dataclass(frozen=True)
class MaskValidationResult:
    mask_dir: Path
    image_count: int
    mask_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "mask_dir": str(self.mask_dir),
            "image_count": self.image_count,
            "mask_count": self.mask_count,
            "complete": self.image_count == self.mask_count,
        }


def validate_capture_mask_png(path: Path, expected_size: tuple[int, int]) -> None:
    """Validate a capture-space mask's format and declared frame dimensions."""
    size, grayscale = _png_dimensions_and_grayscale(path)
    if not grayscale:
        raise MaskValidationError(f"Mask must be an 8-bit grayscale PNG: {path}")
    if size != expected_size:
        raise MaskValidationError(
            f"Mask dimensions {size} do not match frame dimensions {expected_size}: {path}"
        )
    _verify_png_decodes(path, expected_size)


def validate_openmvs_masks(mask_dir: Path, image_dir: Path) -> MaskValidationResult:
    """Require one dimension-matched grayscale PNG mask per undistorted image."""
    if mask_dir.is_symlink():
        raise MaskValidationError(f"Mask directory must not be a symlink: {mask_dir}")
    if image_dir.is_symlink():
        raise MaskValidationError(f"Image directory must not be a symlink: {image_dir}")
    mask_dir = mask_dir.resolve()
    image_dir = image_dir.resolve()
    if not mask_dir.is_dir():
        raise MaskValidationError(f"Mask directory is missing or unsafe: {mask_dir}")
    if not image_dir.is_dir():
        raise MaskValidationError(f"Undistorted image directory is missing or unsafe: {image_dir}")

    images = sorted(
        path for path in image_dir.rglob("*")
        if path.is_file() and not path.is_symlink()
        and path.suffix.lower() in {".jpg", ".jpeg", ".png"}
        and not path.name.lower().endswith(".mask.png")
    )
    if not images:
        raise MaskValidationError(f"No undistorted images found in {image_dir}")

    expected: set[Path] = set()
    for image in images:
        relative = image.relative_to(image_dir)
        mask = mask_dir / relative.with_suffix(".mask.png")
        expected.add(mask)
        if not mask.is_file() or mask.is_symlink():
            raise MaskValidationError(f"Missing mask for {relative.as_posix()}: {mask}")
        image_size = _image_dimensions(image)
        mask_size, grayscale = _png_dimensions_and_grayscale(mask)
        if not grayscale:
            raise MaskValidationError(f"Mask must be an 8-bit grayscale PNG: {mask}")
        if mask_size != image_size:
            raise MaskValidationError(
                f"Mask dimensions {mask_size} do not match image dimensions {image_size}: {mask}"
            )
        _verify_png_decodes(mask, image_size)

    actual = {
        path for path in mask_dir.rglob("*.mask.png")
        if path.is_file() and not path.is_symlink()
    }
    extras = actual - expected
    if extras:
        first = min(extras)
        raise MaskValidationError(f"Unexpected mask without a matching image: {first}")
    return MaskValidationResult(mask_dir, len(images), len(actual))


def stage_openmvs_texture_masks(mask_dir: Path, image_dir: Path) -> MaskValidationResult:
    """Publish validated masks beside images for OpenMVS texture selection."""
    result = validate_openmvs_masks(mask_dir, image_dir)
    image_dir = image_dir.resolve()
    for source in sorted(mask_dir.resolve().rglob("*.mask.png")):
        relative = source.relative_to(mask_dir.resolve())
        destination = image_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink() or (destination.exists() and not destination.is_file()):
            raise MaskValidationError(f"Texture mask destination is unsafe: {destination}")
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            delete=False,
        ) as output:
            temporary = Path(output.name)
            with source.open("rb") as input_file:
                shutil.copyfileobj(input_file, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.replace(temporary, destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    return result


def stage_colmap_fusion_masks(
    mask_dir: Path,
    image_dir: Path,
    output_dir: Path,
) -> MaskValidationResult:
    """Translate OpenMVS mask names to COLMAP's full-image-name convention."""
    result = validate_openmvs_masks(mask_dir, image_dir)
    image_dir = image_dir.resolve()
    output_dir = output_dir.resolve()
    if output_dir.is_symlink():
        raise MaskValidationError(f"COLMAP mask directory is unsafe: {output_dir}")
    if output_dir.exists():
        return _validate_colmap_fusion_masks(mask_dir, image_dir, output_dir, result)

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=output_dir.parent, prefix=".colmap-masks."))
    try:
        for image in _undistorted_images(image_dir):
            relative = image.relative_to(image_dir)
            source = mask_dir.resolve() / relative.with_suffix(".mask.png")
            destination = staging / Path(f"{relative.as_posix()}.png")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        os.rename(staging, output_dir)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return _validate_colmap_fusion_masks(mask_dir, image_dir, output_dir, result)


def _validate_colmap_fusion_masks(
    mask_dir: Path,
    image_dir: Path,
    output_dir: Path,
    source_result: MaskValidationResult,
) -> MaskValidationResult:
    if not output_dir.is_dir() or output_dir.is_symlink():
        raise MaskValidationError(f"COLMAP mask directory is unsafe: {output_dir}")
    expected: set[Path] = set()
    for image in _undistorted_images(image_dir):
        relative = image.relative_to(image_dir)
        destination = output_dir / Path(f"{relative.as_posix()}.png")
        expected.add(destination)
        if not destination.is_file() or destination.is_symlink():
            raise MaskValidationError(f"Missing COLMAP fusion mask: {destination}")
        validate_capture_mask_png(destination, _image_dimensions(image))
    actual = {
        path for path in output_dir.rglob("*.png")
        if path.is_file() and not path.is_symlink()
    }
    if actual != expected:
        raise MaskValidationError("COLMAP fusion mask set contains unexpected files")
    return MaskValidationResult(output_dir, source_result.image_count, len(actual))


def _undistorted_images(image_dir: Path) -> list[Path]:
    return sorted(
        path for path in image_dir.rglob("*")
        if path.is_file() and not path.is_symlink()
        and path.suffix.lower() in {".jpg", ".jpeg", ".png"}
        and not path.name.lower().endswith(".mask.png")
    )


def _image_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as stream:
        signature = stream.read(24)
        if signature.startswith(b"\x89PNG\r\n\x1a\n") and len(signature) >= 24:
            return struct.unpack(">II", signature[16:24])
        if not signature.startswith(b"\xff\xd8"):
            raise MaskValidationError(f"Unsupported image format: {path}")
        stream.seek(2)
        while True:
            marker_start = stream.read(1)
            if not marker_start:
                break
            if marker_start != b"\xff":
                continue
            marker = stream.read(1)
            while marker == b"\xff":
                marker = stream.read(1)
            if marker in {b"\xd8", b"\xd9"}:
                continue
            length_bytes = stream.read(2)
            if len(length_bytes) != 2:
                break
            length = struct.unpack(">H", length_bytes)[0]
            if length < 2:
                break
            if marker and marker[0] in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                data = stream.read(5)
                if len(data) != 5:
                    break
                height, width = struct.unpack(">HH", data[1:5])
                return (width, height)
            stream.seek(length - 2, 1)
    raise MaskValidationError(f"Could not read image dimensions: {path}")


def _png_dimensions_and_grayscale(path: Path) -> tuple[tuple[int, int], bool]:
    with path.open("rb") as stream:
        header = stream.read(29)
        try:
            stream.seek(-12, 2)
        except OSError as error:
            raise MaskValidationError(f"Truncated PNG mask: {path}") from error
        trailer = stream.read(12)
    if len(header) != 29 or not header.startswith(b"\x89PNG\r\n\x1a\n") or header[12:16] != b"IHDR":
        raise MaskValidationError(f"Invalid PNG mask: {path}")
    if len(trailer) != 12 or trailer[:8] != b"\x00\x00\x00\x00IEND":
        raise MaskValidationError(f"PNG mask has no terminal IEND chunk: {path}")
    width, height = struct.unpack(">II", header[16:24])
    bit_depth = header[24]
    color_type = header[25]
    if width < 1 or height < 1:
        raise MaskValidationError(f"Invalid PNG mask dimensions: {path}")
    return (width, height), bit_depth == 8 and color_type == 0


def _verify_png_decodes(path: Path, expected_size: tuple[int, int]) -> None:
    """Verify the complete PNG stream after bounded header checks succeed."""
    try:
        with Image.open(path) as image:
            image_format = image.format
            image_mode = image.mode
            image_size = image.size
            image.verify()
        with Image.open(path) as image:
            image.load()
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as error:
        raise MaskValidationError(f"Unable to decode PNG mask: {path}") from error

    if image_format != "PNG" or image_mode != "L" or image_size != expected_size:
        raise MaskValidationError(f"Mask must decode as an 8-bit grayscale PNG: {path}")
