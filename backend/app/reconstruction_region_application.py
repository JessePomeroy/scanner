"""Apply and verify a reviewed reconstruction region in OpenMVS outputs."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import struct
import tempfile
from typing import BinaryIO, Iterator

from app.reconstruction_region import ReconstructionRegion


class ReconstructionRegionApplicationError(ValueError):
    """Raised when a reviewed region cannot be applied or verified safely."""


@dataclass(frozen=True)
class RegionPointCloudVerification:
    point_count: int
    outside_point_count: int

    def as_dict(self) -> dict[str, int]:
        return {
            "point_count": self.point_count,
            "outside_point_count": self.outside_point_count,
        }


def write_openmvs_roi_file(scan_root: Path, region: ReconstructionRegion) -> Path:
    """Write OpenMVS's text OBB format using its world-to-local rotation."""
    metadata = scan_root.resolve() / "metadata"
    if metadata.is_symlink():
        raise ReconstructionRegionApplicationError("Reconstruction metadata is unsafe")
    metadata.mkdir(parents=True, exist_ok=True)
    path = metadata / "openmvs_region.roi"
    if path.is_symlink():
        raise ReconstructionRegionApplicationError("OpenMVS ROI path is unsafe")

    local_to_world = _quaternion_matrix(region.orientation_xyzw)
    world_to_local = tuple(zip(*local_to_world))
    rows = [*world_to_local, region.center, tuple(value / 2 for value in region.extents)]
    payload = "\n".join(" ".join(format(value, ".17g") for value in row) for row in rows) + "\n"
    _write_text_atomic(path, payload)
    return path


def point_is_in_region(
    point: tuple[float, float, float],
    region: ReconstructionRegion,
    *,
    tolerance: float = 1e-5,
) -> bool:
    rotation = _quaternion_matrix(region.orientation_xyzw)
    return _point_is_in_region(point, region, rotation=rotation, tolerance=tolerance)


def _point_is_in_region(
    point: tuple[float, float, float],
    region: ReconstructionRegion,
    *,
    rotation: tuple[tuple[float, ...], ...],
    tolerance: float,
) -> bool:
    delta = tuple(point[index] - region.center[index] for index in range(3))
    local = tuple(sum(rotation[row][axis] * delta[row] for row in range(3)) for axis in range(3))
    return all(abs(local[axis]) <= region.extents[axis] / 2 + tolerance for axis in range(3))


def verify_point_cloud_in_region(
    path: Path,
    region: ReconstructionRegion,
    *,
    point_hard_limit: int = 10_000_000,
    tolerance: float = 1e-5,
) -> RegionPointCloudVerification:
    """Stream an ASCII or binary PLY and prove every vertex lies in the OBB."""
    if path.is_symlink() or not path.is_file():
        raise ReconstructionRegionApplicationError(f"Point cloud is missing or unsafe: {path}")
    count = 0
    outside = 0
    rotation = _quaternion_matrix(region.orientation_xyzw)
    for point in _iter_ply_vertices(path, point_hard_limit=point_hard_limit):
        count += 1
        if not _point_is_in_region(
            point,
            region,
            rotation=rotation,
            tolerance=tolerance,
        ):
            outside += 1
    if count == 0:
        raise ReconstructionRegionApplicationError(f"Point cloud contains no vertices: {path}")
    if outside:
        raise ReconstructionRegionApplicationError(
            f"Region verification failed: {outside} of {count} vertices are outside the selected region"
        )
    return RegionPointCloudVerification(count, outside)


def record_region_application(
    scan_root: Path,
    region: ReconstructionRegion,
    *,
    roi_path: Path,
    unscoped_point_count: int,
    scoped_verification: RegionPointCloudVerification,
    mesh_verification: RegionPointCloudVerification,
) -> Path:
    path = scan_root.resolve() / "metadata" / "reconstruction_region_application.json"
    removed = unscoped_point_count - scoped_verification.point_count
    if unscoped_point_count < 1 or removed < 0:
        raise ReconstructionRegionApplicationError(
            "Scoped reconstruction point counts are inconsistent"
        )
    payload = {
        "schema_version": "1.0",
        "method": "openmvs_oriented_box",
        "region": region.as_dict(),
        "roi_file": str(roi_path.relative_to(scan_root.resolve())),
        "dense": {
            "unscoped_point_count": unscoped_point_count,
            "scoped_point_count": scoped_verification.point_count,
            "removed_point_count": removed,
            "retained_ratio": scoped_verification.point_count / unscoped_point_count,
            "outside_point_count": scoped_verification.outside_point_count,
        },
        "mesh": mesh_verification.as_dict(),
    }
    _write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return path


def _quaternion_matrix(q: tuple[float, float, float, float]) -> tuple[tuple[float, ...], ...]:
    x, y, z, w = q
    return (
        (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
        (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
        (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)),
    )


_SCALARS = {
    "char": "b", "int8": "b", "uchar": "B", "uint8": "B",
    "short": "h", "int16": "h", "ushort": "H", "uint16": "H",
    "int": "i", "int32": "i", "uint": "I", "uint32": "I",
    "float": "f", "float32": "f", "double": "d", "float64": "d",
}
_INTEGER_SCALARS = {
    "char", "int8", "uchar", "uint8", "short", "int16", "ushort", "uint16",
    "int", "int32", "uint", "uint32",
}


def _iter_ply_vertices(path: Path, *, point_hard_limit: int) -> Iterator[tuple[float, float, float]]:
    with path.open("rb") as file:
        header = []
        header_bytes = 0
        while True:
            line = file.readline()
            header_bytes += len(line)
            if not line or header_bytes > 64 * 1024:
                raise ReconstructionRegionApplicationError(f"Invalid PLY header: {path}")
            try:
                text = line.decode("ascii").strip()
            except UnicodeDecodeError as error:
                raise ReconstructionRegionApplicationError(f"Invalid PLY header: {path}") from error
            header.append(text)
            if text == "end_header":
                break
        if not header or header[0] != "ply":
            raise ReconstructionRegionApplicationError(f"Invalid PLY signature: {path}")
        fmt, elements = _parse_ply_header(header, path)
        vertex_count = next((count for name, count, _ in elements if name == "vertex"), 0)
        if vertex_count > point_hard_limit:
            raise ReconstructionRegionApplicationError(
                f"PLY vertex count {vertex_count} exceeds hard limit {point_hard_limit}"
            )
        if fmt == "ascii":
            yield from _iter_ascii_elements(file, elements, path)
        else:
            endian = "<" if fmt == "binary_little_endian" else ">"
            yield from _iter_binary_elements(file, elements, endian, path)


def _parse_ply_header(lines: list[str], path: Path):
    fmt = None
    elements: list[tuple[str, int, list[tuple[str, ...]]]] = []
    for line in lines[1:]:
        parts = line.split()
        if not parts or parts[0] in {"comment", "obj_info", "end_header"}:
            continue
        if parts[0] == "format" and len(parts) == 3 and parts[2] == "1.0":
            fmt = parts[1]
        elif parts[0] == "element" and len(parts) == 3:
            try:
                count = int(parts[2])
            except ValueError as error:
                raise ReconstructionRegionApplicationError(f"Invalid PLY element count: {path}") from error
            if count < 0:
                raise ReconstructionRegionApplicationError(f"Invalid PLY element count: {path}")
            elements.append((parts[1], count, []))
        elif parts[0] == "property" and elements:
            prop = tuple(parts[1:])
            if (len(prop) == 2 and prop[0] in _SCALARS) or (
                len(prop) == 4
                and prop[0] == "list"
                and prop[1] in _INTEGER_SCALARS
                and prop[2] in _SCALARS
            ):
                elements[-1][2].append(prop)
            else:
                raise ReconstructionRegionApplicationError(f"Unsupported PLY property: {path}")
        else:
            raise ReconstructionRegionApplicationError(f"Invalid PLY header entry: {path}")
    if fmt not in {"ascii", "binary_little_endian", "binary_big_endian"}:
        raise ReconstructionRegionApplicationError(f"Unsupported PLY format: {path}")
    return fmt, elements


def _iter_ascii_elements(file: BinaryIO, elements, path: Path):
    for name, count, properties in elements:
        coordinate_indices = _coordinate_indices(properties, name, path)
        for _ in range(count):
            line = file.readline()
            if not line:
                raise ReconstructionRegionApplicationError(f"Truncated PLY payload: {path}")
            tokens = line.split()
            values = []
            cursor = 0
            try:
                for prop in properties:
                    if len(prop) == 2:
                        values.append(float(tokens[cursor]))
                        cursor += 1
                    else:
                        size = int(tokens[cursor])
                        if size < 0 or size > 1_000_000:
                            raise ValueError
                        cursor += 1 + size
                        values.append(None)
                if cursor != len(tokens):
                    raise ValueError
            except (ValueError, IndexError) as error:
                raise ReconstructionRegionApplicationError(f"Invalid ASCII PLY payload: {path}") from error
            if name == "vertex":
                point = tuple(float(values[index]) for index in coordinate_indices)
                if not all(math.isfinite(value) for value in point):
                    raise ReconstructionRegionApplicationError(f"Non-finite PLY vertex: {path}")
                yield point


def _iter_binary_elements(file: BinaryIO, elements, endian: str, path: Path):
    for name, count, properties in elements:
        coordinate_indices = _coordinate_indices(properties, name, path)
        for _ in range(count):
            values = []
            for prop in properties:
                if len(prop) == 2:
                    values.append(_read_scalar(file, endian, prop[0], path))
                else:
                    size = int(_read_scalar(file, endian, prop[1], path))
                    if size < 0 or size > 1_000_000:
                        raise ReconstructionRegionApplicationError(f"Invalid PLY list size: {path}")
                    scalar = struct.Struct(endian + _SCALARS[prop[2]])
                    payload = file.read(scalar.size * size)
                    if len(payload) != scalar.size * size:
                        raise ReconstructionRegionApplicationError(f"Truncated PLY payload: {path}")
                    values.append(None)
            if name == "vertex":
                point = tuple(float(values[index]) for index in coordinate_indices)
                if not all(math.isfinite(value) for value in point):
                    raise ReconstructionRegionApplicationError(f"Non-finite PLY vertex: {path}")
                yield point


def _coordinate_indices(properties, element: str, path: Path) -> tuple[int, int, int]:
    if element != "vertex":
        return (0, 0, 0)
    names = [prop[-1] for prop in properties]
    try:
        return names.index("x"), names.index("y"), names.index("z")
    except ValueError as error:
        raise ReconstructionRegionApplicationError(f"PLY vertex coordinates are missing: {path}") from error


def _read_scalar(file: BinaryIO, endian: str, type_name: str, path: Path):
    scalar = struct.Struct(endian + _SCALARS[type_name])
    payload = file.read(scalar.size)
    if len(payload) != scalar.size:
        raise ReconstructionRegionApplicationError(f"Truncated PLY payload: {path}")
    return scalar.unpack(payload)[0]


def _write_text_atomic(path: Path, payload: str) -> None:
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
