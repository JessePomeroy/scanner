"""Versioned contract for a user-reviewed 3D reconstruction region."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import fcntl
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Literal


RegionShape = Literal["oriented_box"]
RegionCoordinateSystem = Literal["colmap_reconstruction"]
RegionSource = Literal[
    "user_sparse_preview",
    "automatic",
    "arkit_alignment",
    "imported",
]

SCHEMA_VERSION = "1.0"
_SUPPORTED_SHAPES = {"oriented_box"}
_SUPPORTED_COORDINATE_SYSTEMS = {"colmap_reconstruction"}
_SUPPORTED_SOURCES = {
    "user_sparse_preview",
    "automatic",
    "arkit_alignment",
    "imported",
}
_FIELDS = {
    "schema_version",
    "shape",
    "coordinate_system",
    "center",
    "extents",
    "orientation_xyzw",
    "source",
    "revision",
}
_QUATERNION_NORM_TOLERANCE = 1e-4
_MAX_REGION_BYTES = 64 * 1024
REGION_RELATIVE_PATH = Path("metadata/reconstruction_region.json")


class ReconstructionRegionError(ValueError):
    """Raised when reconstruction-region data is incomplete or unsafe."""


class ReconstructionRegionNotFoundError(ReconstructionRegionError):
    """Raised when a reviewed region has not been stored for a scan."""


class ReconstructionRegionRevisionError(ReconstructionRegionError):
    """Raised when a region write would skip or overwrite a revision."""


@dataclass(frozen=True)
class ReconstructionRegion:
    """An oriented box in the coordinate system produced by COLMAP.

    ``extents`` are the box's full edge lengths along its local X, Y, and Z
    axes. ``orientation_xyzw`` is a unit quaternion that rotates those local
    axes into the COLMAP reconstruction coordinate system.
    """

    center: tuple[float, float, float]
    extents: tuple[float, float, float]
    orientation_xyzw: tuple[float, float, float, float]
    source: RegionSource
    revision: int
    schema_version: str = SCHEMA_VERSION
    shape: RegionShape = "oriented_box"
    coordinate_system: RegionCoordinateSystem = "colmap_reconstruction"

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ReconstructionRegionError(
                f"Unsupported reconstruction-region schema version: {self.schema_version!r}"
            )
        if self.shape not in _SUPPORTED_SHAPES:
            raise ReconstructionRegionError(
                f"Unsupported reconstruction-region shape: {self.shape!r}"
            )
        if self.coordinate_system not in _SUPPORTED_COORDINATE_SYSTEMS:
            raise ReconstructionRegionError(
                "Unsupported reconstruction-region coordinate system: "
                f"{self.coordinate_system!r}"
            )
        if self.source not in _SUPPORTED_SOURCES:
            raise ReconstructionRegionError(
                f"Unsupported reconstruction-region source: {self.source!r}"
            )
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise ReconstructionRegionError("Reconstruction-region revision must be an integer")
        if self.revision < 1:
            raise ReconstructionRegionError("Reconstruction-region revision must be positive")

        _validate_vector("center", self.center, length=3)
        _validate_vector("extents", self.extents, length=3)
        if any(value <= 0 for value in self.extents):
            raise ReconstructionRegionError("Reconstruction-region extents must be positive")
        _validate_vector("orientation_xyzw", self.orientation_xyzw, length=4)
        quaternion_norm = math.sqrt(sum(value * value for value in self.orientation_xyzw))
        if abs(quaternion_norm - 1.0) > _QUATERNION_NORM_TOLERANCE:
            raise ReconstructionRegionError(
                "Reconstruction-region orientation_xyzw must be a unit quaternion"
            )

    def as_dict(self) -> dict[str, Any]:
        """Return the stable cross-platform JSON representation."""
        return {
            "schema_version": self.schema_version,
            "shape": self.shape,
            "coordinate_system": self.coordinate_system,
            "center": list(self.center),
            "extents": list(self.extents),
            "orientation_xyzw": list(self.orientation_xyzw),
            "source": self.source,
            "revision": self.revision,
        }

    def to_json(self, *, indent: int | None = None) -> str:
        """Serialize deterministically for job records and sidecar reports."""
        return json.dumps(self.as_dict(), indent=indent, sort_keys=True, allow_nan=False)

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ReconstructionRegion":
        """Parse a strict region payload without silently ignoring misspelled fields."""
        missing = sorted(_FIELDS - payload.keys())
        if missing:
            raise ReconstructionRegionError(
                f"Reconstruction-region fields are missing: {', '.join(missing)}"
            )
        unexpected = sorted(payload.keys() - _FIELDS)
        if unexpected:
            raise ReconstructionRegionError(
                f"Unexpected reconstruction-region fields: {', '.join(unexpected)}"
            )

        return cls(
            schema_version=_parse_string(payload, "schema_version"),
            shape=_parse_string(payload, "shape"),  # type: ignore[arg-type]
            coordinate_system=_parse_string(payload, "coordinate_system"),  # type: ignore[arg-type]
            center=_parse_vector(payload, "center", length=3),  # type: ignore[arg-type]
            extents=_parse_vector(payload, "extents", length=3),  # type: ignore[arg-type]
            orientation_xyzw=_parse_vector(  # type: ignore[arg-type]
                payload,
                "orientation_xyzw",
                length=4,
            ),
            source=_parse_string(payload, "source"),  # type: ignore[arg-type]
            revision=_parse_revision(payload.get("revision")),
        )

    @classmethod
    def from_json(cls, raw: str) -> "ReconstructionRegion":
        """Parse a JSON object into a validated region contract."""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ReconstructionRegionError("Invalid reconstruction-region JSON") from error
        if not isinstance(payload, dict):
            raise ReconstructionRegionError("Reconstruction-region JSON must contain an object")
        return cls.from_dict(payload)


def _parse_string(payload: Mapping[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ReconstructionRegionError(f"Reconstruction-region {name} must be a string")
    return value


def _parse_vector(
    payload: Mapping[str, object],
    name: str,
    *,
    length: int,
) -> tuple[float, ...]:
    value = payload.get(name)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ReconstructionRegionError(
            f"Reconstruction-region {name} must contain {length} numbers"
        )
    parsed = tuple(_parse_number(item, name) for item in value)
    if len(parsed) != length:
        raise ReconstructionRegionError(
            f"Reconstruction-region {name} must contain {length} numbers"
        )
    return parsed


def _validate_vector(name: str, value: Sequence[object], *, length: int) -> None:
    if len(value) != length:
        raise ReconstructionRegionError(
            f"Reconstruction-region {name} must contain {length} numbers"
        )
    for item in value:
        _parse_number(item, name)


def _parse_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReconstructionRegionError(
            f"Reconstruction-region {name} must contain only numbers"
        )
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ReconstructionRegionError(
            f"Reconstruction-region {name} must contain only finite numbers"
        )
    return parsed


def _parse_revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReconstructionRegionError("Reconstruction-region revision must be an integer")
    return value


def load_reconstruction_region(scan_root: Path) -> ReconstructionRegion:
    """Load a stored region without following symbolic links or oversized data."""
    path = _region_path(scan_root)
    if path.is_symlink():
        raise ReconstructionRegionError(f"Reconstruction region must not be a symlink: {path}")
    try:
        file_stat = path.stat()
    except FileNotFoundError as error:
        raise ReconstructionRegionNotFoundError(
            f"Reconstruction region has not been selected: {path}"
        ) from error
    if not path.is_file() or file_stat.st_size > _MAX_REGION_BYTES:
        raise ReconstructionRegionError(f"Reconstruction region is missing or unsafe: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ReconstructionRegionError(f"Unable to read reconstruction region: {path}") from error
    return ReconstructionRegion.from_json(raw)


def save_reconstruction_region(scan_root: Path, region: ReconstructionRegion) -> Path:
    """Atomically store exactly the first, next, or same idempotent revision."""
    path = _region_path(scan_root, create_metadata=True)
    lock_path = path.parent / ".reconstruction_region.lock"
    if lock_path.is_symlink():
        raise ReconstructionRegionError(
            f"Reconstruction-region lock must not be a symlink: {lock_path}"
        )
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        lock_fd = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise ReconstructionRegionError(
            f"Unable to open reconstruction-region lock: {lock_path}"
        ) from error
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        current = _load_optional_region(scan_root)
        if current is None:
            if region.revision != 1:
                raise ReconstructionRegionRevisionError(
                    "The first reconstruction-region revision must be 1"
                )
        elif region.revision == current.revision:
            if region == current:
                return path
            raise ReconstructionRegionRevisionError(
                f"Reconstruction-region revision {region.revision} already exists"
            )
        elif region.revision != current.revision + 1:
            raise ReconstructionRegionRevisionError(
                "Reconstruction-region revision must advance exactly one step "
                f"from {current.revision}"
            )

        _write_region_atomic(path, region)
        return path
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _region_path(scan_root: Path, *, create_metadata: bool = False) -> Path:
    scan_root = scan_root.resolve()
    metadata_dir = scan_root / "metadata"
    if metadata_dir.is_symlink():
        raise ReconstructionRegionError(
            f"Reconstruction metadata must not be a symlink: {metadata_dir}"
        )
    if create_metadata:
        metadata_dir.mkdir(parents=True, exist_ok=True)
    if not metadata_dir.is_dir():
        raise ReconstructionRegionError(
            f"Reconstruction metadata directory is missing: {metadata_dir}"
        )
    return metadata_dir / REGION_RELATIVE_PATH.name


def _load_optional_region(scan_root: Path) -> ReconstructionRegion | None:
    try:
        return load_reconstruction_region(scan_root)
    except ReconstructionRegionNotFoundError:
        return None


def _write_region_atomic(path: Path, region: ReconstructionRegion) -> None:
    if path.is_symlink():
        raise ReconstructionRegionError(f"Reconstruction region must not be a symlink: {path}")
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
            temporary.write(region.to_json(indent=2))
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
