"""Destructively filter Gaussian PLY primitives for verified publication."""

from __future__ import annotations

from dataclasses import dataclass
from bisect import bisect_right
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import tempfile
from typing import BinaryIO, Iterator


class GaussianCleanupError(ValueError):
    """Raised when a Gaussian cleanup recipe or PLY is unsafe or invalid."""


@dataclass(frozen=True)
class GaussianCrop:
    shape: str
    center: tuple[float, float, float]
    keep: str
    size: tuple[float, float, float] | None = None
    radius: float | None = None
    height: float | None = None


@dataclass(frozen=True)
class PrimitiveSelection:
    mode: str
    ranges: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class GaussianCleanupRecipe:
    schema_version: str
    revision: int = 1
    crop: GaussianCrop | None = None
    selection: PrimitiveSelection | None = None


@dataclass(frozen=True)
class PlyHeader:
    raw_lines: tuple[bytes, ...]
    format_name: str
    vertex_count: int
    properties: tuple[tuple[str, str], ...]
    header_size: int


_SCALARS = {
    "char": "b", "int8": "b", "uchar": "B", "uint8": "B",
    "short": "h", "int16": "h", "ushort": "H", "uint16": "H",
    "int": "i", "int32": "i", "uint": "I", "uint32": "I",
    "float": "f", "float32": "f", "double": "d", "float64": "d",
}


def load_gaussian_cleanup_recipe(path: Path) -> GaussianCleanupRecipe:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise GaussianCleanupError(f"Unable to read Gaussian cleanup recipe: {path}") from error
    if not isinstance(payload, dict):
        raise GaussianCleanupError("Gaussian cleanup recipe must be an object")
    _reject_unknown(payload, {"schema_version", "revision", "crop", "selection"}, "recipe")
    if payload.get("schema_version") != "1.0":
        raise GaussianCleanupError("Gaussian cleanup schema_version must be 1.0")
    revision = payload.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise GaussianCleanupError("Gaussian cleanup revision must be a positive integer")
    crop = _parse_crop(payload.get("crop")) if payload.get("crop") is not None else None
    selection = (
        _parse_selection(payload.get("selection"))
        if payload.get("selection") is not None
        else None
    )
    if crop is None and selection is None:
        raise GaussianCleanupError("Gaussian cleanup must define crop or selection")
    return GaussianCleanupRecipe(
        "1.0",
        revision=revision,
        crop=crop,
        selection=selection,
    )


def cleanup_gaussian_ply(
    source: Path,
    output: Path,
    recipe: GaussianCleanupRecipe,
    *,
    report_path: Path,
    primitive_hard_limit: int = 50_000_000,
    overwrite: bool = False,
) -> Path:
    source_candidate = source.absolute()
    if source_candidate.is_symlink() or not source_candidate.is_file():
        raise GaussianCleanupError(f"Gaussian source PLY is missing or unsafe: {source_candidate}")
    source = source_candidate.resolve()
    output = output.absolute()
    report_path = report_path.absolute()
    if output == source or report_path == source or output == report_path:
        raise GaussianCleanupError("Gaussian source, output, and report paths must be distinct")
    if output.is_symlink() or report_path.is_symlink():
        raise GaussianCleanupError("Gaussian output or report path is unsafe")
    if not overwrite and (output.exists() or report_path.exists()):
        raise GaussianCleanupError("Gaussian output and report paths must not already exist")
    if any(path.exists() and not path.is_file() for path in (output, report_path)):
        raise GaussianCleanupError("Gaussian output and report paths must be regular files")
    if any(path.exists() and os.path.samefile(path, source) for path in (output, report_path)):
        raise GaussianCleanupError("Gaussian output or report aliases the immutable source")
    output.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    header = read_gaussian_ply_header(source, primitive_hard_limit=primitive_hard_limit)
    source_identity = _file_identity(source)
    _validate_selection_bounds(recipe.selection, header.vertex_count)
    retained_count = sum(
        _primitive_is_retained(index, point, recipe)
        for index, point, _ in iter_gaussian_records(source, header)
    )
    if retained_count == 0:
        raise GaussianCleanupError("Gaussian cleanup removed every primitive")
    if _file_identity(source) != source_identity:
        raise GaussianCleanupError("Gaussian source changed during cleanup inspection")

    temporary_output: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_output = Path(stream.name)
            stream.writelines(_header_with_vertex_count(header, retained_count))
            written = 0
            for index, point, raw_record in iter_gaussian_records(source, header):
                if _primitive_is_retained(index, point, recipe):
                    stream.write(raw_record)
                    written += 1
            if written != retained_count:
                raise GaussianCleanupError("Gaussian retained count changed while writing")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_output, output)
        temporary_output = None
    finally:
        if temporary_output is not None:
            temporary_output.unlink(missing_ok=True)
    if _file_identity(source) != source_identity:
        output.unlink(missing_ok=True)
        report_path.unlink(missing_ok=True)
        raise GaussianCleanupError("Gaussian source changed while publishing cleanup output")

    output_header = read_gaussian_ply_header(output, primitive_hard_limit=primitive_hard_limit)
    outside_count = 0
    verified_count = 0
    for _, point, _ in iter_gaussian_records(output, output_header):
        verified_count += 1
        if recipe.crop is not None and not point_is_retained(point, recipe.crop):
            outside_count += 1
    if verified_count != retained_count or outside_count:
        output.unlink(missing_ok=True)
        report_path.unlink(missing_ok=True)
        raise GaussianCleanupError("Published Gaussian PLY failed retained-primitive verification")

    report = {
        "schema_version": "1.0",
        "artifact_type": "gaussian_ply",
        "cleanup_revision": recipe.revision,
        "effective_bounds": gaussian_crop_payload(recipe.crop),
        "method": "destructive_gaussian_ply_filter",
        "source_ply": str(source),
        "output_ply": str(output),
        "recipe": gaussian_cleanup_recipe_payload(recipe),
        "source_primitive_count": header.vertex_count,
        "retained_primitive_count": retained_count,
        "removed_primitive_count": header.vertex_count - retained_count,
        "retained_ratio": retained_count / header.vertex_count,
        "outside_crop_primitive_count": outside_count,
        "output_sha256": _sha256(output),
        "source_sha256": _sha256(source),
        "source_preserved": True,
        "destructive_output_verified": True,
        "overwrite_enabled": overwrite,
    }
    try:
        _write_json_atomic(report_path, report)
    except BaseException:
        output.unlink(missing_ok=True)
        report_path.unlink(missing_ok=True)
        raise
    return output


def read_gaussian_ply_header(
    path: Path,
    *,
    primitive_hard_limit: int,
) -> PlyHeader:
    raw_lines: list[bytes] = []
    header_size = 0
    with path.open("rb") as stream:
        while True:
            line = stream.readline()
            header_size += len(line)
            if not line or header_size > 64 * 1024:
                raise GaussianCleanupError(f"Invalid Gaussian PLY header: {path}")
            raw_lines.append(line)
            if line.strip() == b"end_header":
                break
    try:
        lines = [line.decode("ascii").strip() for line in raw_lines]
    except UnicodeDecodeError as error:
        raise GaussianCleanupError(f"Invalid Gaussian PLY header: {path}") from error
    if not lines or lines[0] != "ply":
        raise GaussianCleanupError(f"Invalid Gaussian PLY signature: {path}")

    format_name: str | None = None
    vertex_count: int | None = None
    properties: list[tuple[str, str]] = []
    active_element: str | None = None
    non_vertex_elements = 0
    saw_vertex = False
    for line in lines[1:]:
        parts = line.split()
        if not parts or parts[0] in {"comment", "obj_info", "end_header"}:
            continue
        if parts[0] == "format" and len(parts) == 3 and parts[2] == "1.0":
            format_name = parts[1]
        elif parts[0] == "element" and len(parts) == 3:
            try:
                count = int(parts[2])
            except ValueError as error:
                raise GaussianCleanupError(f"Invalid Gaussian PLY element: {path}") from error
            if count < 0:
                raise GaussianCleanupError(f"Invalid Gaussian PLY element: {path}")
            active_element = parts[1]
            if active_element == "vertex" and not saw_vertex:
                vertex_count = count
                saw_vertex = True
            elif active_element == "vertex":
                raise GaussianCleanupError("Gaussian PLY contains multiple vertex elements")
            elif count:
                non_vertex_elements += 1
        elif parts[0] == "property" and active_element == "vertex":
            if len(parts) != 3 or parts[1] not in _SCALARS:
                raise GaussianCleanupError("Gaussian vertex properties must be fixed-size scalars")
            properties.append((parts[1], parts[2]))
        else:
            raise GaussianCleanupError(f"Unsupported Gaussian PLY header entry: {line}")
    if format_name not in {"ascii", "binary_little_endian", "binary_big_endian"}:
        raise GaussianCleanupError(f"Unsupported Gaussian PLY format: {format_name}")
    if vertex_count is None or vertex_count < 1 or vertex_count > primitive_hard_limit:
        raise GaussianCleanupError("Gaussian PLY has an invalid primitive count")
    if non_vertex_elements:
        raise GaussianCleanupError("Gaussian cleanup only supports vertex-only PLY files")
    names = [name for _, name in properties]
    if not all(name in names for name in ("x", "y", "z")):
        raise GaussianCleanupError("Gaussian PLY is missing x/y/z properties")
    return PlyHeader(tuple(raw_lines), format_name, vertex_count, tuple(properties), header_size)


def iter_gaussian_records(
    path: Path,
    header: PlyHeader,
) -> Iterator[tuple[int, tuple[float, float, float], bytes]]:
    coordinate_indices = tuple(
        [name for _, name in header.properties].index(axis)
        for axis in ("x", "y", "z")
    )
    with path.open("rb") as stream:
        stream.seek(header.header_size)
        if header.format_name == "ascii":
            for index in range(header.vertex_count):
                line = stream.readline()
                if not line:
                    raise GaussianCleanupError(f"Truncated Gaussian PLY payload: {path}")
                tokens = line.split()
                if len(tokens) != len(header.properties):
                    raise GaussianCleanupError(f"Invalid Gaussian ASCII record: {path}")
                try:
                    point = tuple(float(tokens[item]) for item in coordinate_indices)
                except ValueError as error:
                    raise GaussianCleanupError(f"Invalid Gaussian coordinate: {path}") from error
                _validate_point(point, path)
                yield index, point, line
            if stream.read().strip():
                raise GaussianCleanupError(f"Unexpected trailing Gaussian PLY data: {path}")
            return

        endian = "<" if header.format_name == "binary_little_endian" else ">"
        record = struct.Struct(endian + "".join(_SCALARS[type_name] for type_name, _ in header.properties))
        for index in range(header.vertex_count):
            raw = stream.read(record.size)
            if len(raw) != record.size:
                raise GaussianCleanupError(f"Truncated Gaussian PLY payload: {path}")
            values = record.unpack(raw)
            point = tuple(float(values[item]) for item in coordinate_indices)
            _validate_point(point, path)
            yield index, point, raw
        if stream.read(1):
            raise GaussianCleanupError(f"Unexpected trailing Gaussian PLY data: {path}")


def point_is_retained(point: tuple[float, float, float], crop: GaussianCrop) -> bool:
    offset = tuple(point[index] - crop.center[index] for index in range(3))
    if crop.shape == "box":
        assert crop.size is not None
        inside = all(abs(offset[index]) <= crop.size[index] / 2 for index in range(3))
    else:
        assert crop.radius is not None and crop.height is not None
        inside = math.hypot(offset[0], offset[1]) <= crop.radius and abs(offset[2]) <= crop.height / 2
    return inside if crop.keep == "inside" else not inside


def gaussian_cleanup_recipe_payload(recipe: GaussianCleanupRecipe) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": recipe.schema_version,
        "revision": recipe.revision,
    }
    if recipe.crop is not None:
        payload["crop"] = gaussian_crop_payload(recipe.crop)
    if recipe.selection is not None:
        payload["selection"] = {
            "mode": recipe.selection.mode,
            "ranges": [list(item) for item in recipe.selection.ranges],
        }
    return payload


def gaussian_crop_payload(crop_recipe: GaussianCrop | None) -> dict[str, object] | None:
    if crop_recipe is None:
        return None
    crop: dict[str, object] = {
        "shape": crop_recipe.shape,
        "center": list(crop_recipe.center),
        "keep": crop_recipe.keep,
    }
    if crop_recipe.size is not None:
        crop["size"] = list(crop_recipe.size)
    if crop_recipe.radius is not None:
        crop["radius"] = crop_recipe.radius
    if crop_recipe.height is not None:
        crop["height"] = crop_recipe.height
    return crop


def _parse_crop(payload: object) -> GaussianCrop:
    if not isinstance(payload, dict):
        raise GaussianCleanupError("Gaussian crop must be an object")
    _reject_unknown(payload, {"shape", "center", "keep", "size", "radius", "height"}, "crop")
    shape = payload.get("shape")
    keep = payload.get("keep")
    if shape not in {"box", "cylinder"} or keep not in {"inside", "outside"}:
        raise GaussianCleanupError("Gaussian crop shape/keep is invalid")
    center = _vector3(payload.get("center"), "crop center")
    if shape == "box":
        size = _vector3(payload.get("size"), "box size", positive=True)
        if payload.get("radius") is not None or payload.get("height") is not None:
            raise GaussianCleanupError("Box crop cannot define radius or height")
        return GaussianCrop(shape, center, keep, size=size)
    radius = _number(payload.get("radius"), "cylinder radius", positive=True)
    height = _number(payload.get("height"), "cylinder height", positive=True)
    if payload.get("size") is not None:
        raise GaussianCleanupError("Cylinder crop cannot define size")
    return GaussianCrop(shape, center, keep, radius=radius, height=height)


def _parse_selection(payload: object) -> PrimitiveSelection:
    if not isinstance(payload, dict):
        raise GaussianCleanupError("Gaussian selection must be an object")
    _reject_unknown(payload, {"mode", "ranges"}, "selection")
    mode = payload.get("mode")
    ranges = payload.get("ranges")
    if mode not in {"keep", "discard"} or not isinstance(ranges, list) or not ranges:
        raise GaussianCleanupError("Gaussian selection mode/ranges is invalid")
    parsed: list[tuple[int, int]] = []
    for item in ranges:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or any(not isinstance(value, int) or isinstance(value, bool) for value in item)
            or item[0] < 0
            or item[1] <= item[0]
        ):
            raise GaussianCleanupError("Selection ranges must be non-empty [start, end) integers")
        parsed.append((item[0], item[1]))
    if parsed != sorted(parsed) or any(parsed[index][0] < parsed[index - 1][1] for index in range(1, len(parsed))):
        raise GaussianCleanupError("Selection ranges must be sorted and non-overlapping")
    return PrimitiveSelection(mode, tuple(parsed))


def _primitive_is_retained(
    index: int,
    point: tuple[float, float, float],
    recipe: GaussianCleanupRecipe,
) -> bool:
    if recipe.crop is not None and not point_is_retained(point, recipe.crop):
        return False
    if recipe.selection is None:
        return True
    position = bisect_right(recipe.selection.ranges, (index, math.inf)) - 1
    selected = (
        position >= 0
        and recipe.selection.ranges[position][0] <= index < recipe.selection.ranges[position][1]
    )
    return selected if recipe.selection.mode == "keep" else not selected


def _validate_selection_bounds(selection: PrimitiveSelection | None, count: int) -> None:
    if selection is not None and selection.ranges[-1][1] > count:
        raise GaussianCleanupError("Gaussian selection range exceeds the source primitive count")


def _header_with_vertex_count(header: PlyHeader, count: int) -> tuple[bytes, ...]:
    result = []
    replaced = False
    for line in header.raw_lines:
        if line.decode("ascii").strip().startswith("element vertex "):
            ending = b"\r\n" if line.endswith(b"\r\n") else b"\n"
            result.append(f"element vertex {count}".encode("ascii") + ending)
            replaced = True
        else:
            result.append(line)
    if not replaced:
        raise GaussianCleanupError("Gaussian PLY vertex header disappeared")
    return tuple(result)


def _validate_point(point: tuple[float, float, float], path: Path) -> None:
    if not all(math.isfinite(value) for value in point):
        raise GaussianCleanupError(f"Non-finite Gaussian coordinate: {path}")


def _vector3(value: object, label: str, *, positive: bool = False) -> tuple[float, float, float]:
    if not isinstance(value, list) or len(value) != 3:
        raise GaussianCleanupError(f"{label} must contain three numbers")
    result = tuple(_number(item, label, positive=positive) for item in value)
    return (result[0], result[1], result[2])


def _number(value: object, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GaussianCleanupError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise GaussianCleanupError(f"{label} must be finite{' and positive' if positive else ''}")
    return result


def _reject_unknown(payload: dict, allowed: set[str], label: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise GaussianCleanupError(f"Unknown Gaussian {label} field(s): {', '.join(unknown)}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_identity(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
