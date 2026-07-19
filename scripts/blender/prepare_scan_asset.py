"""Import reconstruction output into Blender and save a prepared scene.

Run with Blender:

blender --background --python scripts/blender/prepare_scan_asset.py -- \
  input.(obj|ply|glb|gltf) output.blend
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
import sys
from typing import Any


SUPPORTED_IMPORT_SUFFIXES = {".obj", ".ply", ".glb", ".gltf"}


@dataclass(frozen=True)
class BlenderAssetOptions:
    input_path: Path
    output_path: Path
    scale: float = 1.0
    decimate_ratio: float | None = None
    texture_dir: Path | None = None
    export_glb: Path | None = None
    origin: str = "geometry"
    set_units: str = "METRIC"
    cleanup_recipe: Path | None = None
    cleanup_report: Path | None = None


@dataclass(frozen=True)
class MeshCrop:
    shape: str
    center: tuple[float, float, float]
    keep: str
    size: tuple[float, float, float] | None = None
    radius: float | None = None
    height: float | None = None


@dataclass(frozen=True)
class LooseComponentRule:
    keep_largest: int | None = None
    minimum_vertices: int = 1


@dataclass(frozen=True)
class MeshCleanupRecipe:
    schema_version: str
    crop: MeshCrop | None = None
    loose_components: LooseComponentRule | None = None


def main() -> None:
    options = parse_blender_args(blender_script_args(sys.argv))
    prepare_asset(options)


def blender_script_args(argv: list[str]) -> list[str]:
    return argv[argv.index("--") + 1 :] if "--" in argv else []


def parse_blender_args(args: list[str]) -> BlenderAssetOptions:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Input .obj, .ply, .glb, or .gltf file.")
    parser.add_argument("output", type=Path, help="Output .blend file.")
    parser.add_argument("--scale", type=float, default=1.0, help="Uniform scale applied to imported objects.")
    parser.add_argument(
        "--decimate-ratio",
        type=float,
        default=None,
        help="Optional Blender decimate modifier ratio between 0 and 1.",
    )
    parser.add_argument("--texture-dir", type=Path, default=None, help="Optional folder for relinking image textures.")
    parser.add_argument("--export-glb", type=Path, default=None, help="Optional GLB export path after saving .blend.")
    parser.add_argument(
        "--cleanup-recipe",
        type=Path,
        default=None,
        help="Optional versioned JSON crop/component recipe applied to retained mesh copies.",
    )
    parser.add_argument(
        "--cleanup-report",
        type=Path,
        default=None,
        help="Required with --cleanup-recipe; records source/retained mesh evidence.",
    )
    parser.add_argument(
        "--origin",
        choices=["geometry", "cursor", "none"],
        default="geometry",
        help="Origin placement strategy for imported objects.",
    )
    parser.add_argument(
        "--set-units",
        choices=["NONE", "METRIC", "IMPERIAL", "none", "metric", "imperial"],
        default="METRIC",
        help="Scene unit system, or NONE to leave unchanged.",
    )
    parsed = parser.parse_args(args)

    if parsed.input.suffix.lower() not in SUPPORTED_IMPORT_SUFFIXES:
        parser.error(f"Unsupported input format: {parsed.input}")
    if parsed.scale <= 0:
        parser.error("--scale must be positive")
    if parsed.decimate_ratio is not None and not 0 < parsed.decimate_ratio <= 1:
        parser.error("--decimate-ratio must be greater than 0 and less than or equal to 1")
    if (parsed.cleanup_recipe is None) != (parsed.cleanup_report is None):
        parser.error("--cleanup-recipe and --cleanup-report must be provided together")

    return BlenderAssetOptions(
        input_path=parsed.input,
        output_path=parsed.output,
        scale=parsed.scale,
        decimate_ratio=parsed.decimate_ratio,
        texture_dir=parsed.texture_dir,
        export_glb=parsed.export_glb,
        origin=parsed.origin,
        set_units=parsed.set_units.upper(),
        cleanup_recipe=parsed.cleanup_recipe,
        cleanup_report=parsed.cleanup_report,
    )


def prepare_asset(options: BlenderAssetOptions) -> None:
    try:
        import bpy
    except ImportError as error:
        raise SystemExit("This script must be run with Blender's Python runtime.") from error

    options.output_path.parent.mkdir(parents=True, exist_ok=True)
    if options.export_glb is not None:
        options.export_glb.parent.mkdir(parents=True, exist_ok=True)
    if options.cleanup_report is not None:
        options.cleanup_report.parent.mkdir(parents=True, exist_ok=True)

    cleanup_recipe = (
        load_cleanup_recipe(options.cleanup_recipe)
        if options.cleanup_recipe is not None
        else None
    )

    clear_scene(bpy)
    imported_objects = import_asset(bpy, options.input_path)
    normalize_scene_names(bpy, options.input_path.stem)
    configure_units(bpy, options.set_units)
    apply_scale(bpy, imported_objects, options.scale)
    set_origins(bpy, imported_objects, options.origin)
    cleanup_evidence = None
    retained_objects = imported_objects
    if cleanup_recipe is not None:
        retained_objects, cleanup_evidence = apply_reversible_cleanup(
            bpy,
            imported_objects,
            cleanup_recipe,
        )
    if options.decimate_ratio is not None:
        apply_decimation(bpy, retained_objects, options.decimate_ratio)
    if cleanup_recipe is not None and cleanup_evidence is not None:
        cleanup_evidence = finalize_cleanup_evidence(
            retained_objects,
            cleanup_recipe,
            cleanup_evidence,
        )
    if options.texture_dir is not None:
        relink_textures(bpy, options.texture_dir)

    bpy.ops.wm.save_as_mainfile(filepath=str(options.output_path))
    if options.export_glb is not None:
        if cleanup_recipe is not None:
            select_only(bpy, retained_objects)
            bpy.ops.export_scene.gltf(
                filepath=str(options.export_glb),
                export_format="GLB",
                use_selection=True,
            )
        else:
            bpy.ops.export_scene.gltf(filepath=str(options.export_glb), export_format="GLB")
    if options.cleanup_report is not None and cleanup_evidence is not None:
        cleanup_evidence["blend_saved"] = True
        cleanup_evidence["glb_exported"] = options.export_glb is not None
        cleanup_evidence["glb_export_selection_only"] = options.export_glb is not None
        options.cleanup_report.write_text(json.dumps(cleanup_evidence, indent=2, sort_keys=True))


def load_cleanup_recipe(path: Path) -> MeshCleanupRecipe:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"Unable to read cleanup recipe {path}: {error}") from error
    if not isinstance(payload, dict):
        raise SystemExit("Cleanup recipe must be a JSON object")
    _require_recipe_keys(payload, {"schema_version", "crop", "loose_components"}, "recipe")
    if payload.get("schema_version") != "1.0":
        raise SystemExit("Cleanup recipe schema_version must be 1.0")

    crop_payload = payload.get("crop")
    crop = _parse_mesh_crop(crop_payload) if crop_payload is not None else None
    components_payload = payload.get("loose_components")
    components = (
        _parse_loose_component_rule(components_payload)
        if components_payload is not None
        else None
    )
    if crop is None and components is None:
        raise SystemExit("Cleanup recipe must define crop or loose_components")
    return MeshCleanupRecipe("1.0", crop=crop, loose_components=components)


def _parse_mesh_crop(payload: Any) -> MeshCrop:
    if not isinstance(payload, dict):
        raise SystemExit("cleanup crop must be an object")
    _require_recipe_keys(
        payload,
        {"shape", "center", "keep", "size", "radius", "height"},
        "crop",
    )
    shape = payload.get("shape")
    keep = payload.get("keep")
    if shape not in {"box", "cylinder"}:
        raise SystemExit("cleanup crop shape must be box or cylinder")
    if keep not in {"inside", "outside"}:
        raise SystemExit("cleanup crop keep must be inside or outside")
    center = _finite_vector3(payload.get("center"), "crop center")
    if shape == "box":
        size = _positive_vector3(payload.get("size"), "box crop size")
        if payload.get("radius") is not None or payload.get("height") is not None:
            raise SystemExit("box crop cannot define radius or height")
        return MeshCrop(shape, center, keep, size=size)

    radius = _positive_number(payload.get("radius"), "cylinder crop radius")
    height = _positive_number(payload.get("height"), "cylinder crop height")
    if payload.get("size") is not None:
        raise SystemExit("cylinder crop cannot define size")
    return MeshCrop(shape, center, keep, radius=radius, height=height)


def _parse_loose_component_rule(payload: Any) -> LooseComponentRule:
    if not isinstance(payload, dict):
        raise SystemExit("loose_components must be an object")
    _require_recipe_keys(payload, {"keep_largest", "minimum_vertices"}, "loose_components")
    keep_largest = payload.get("keep_largest")
    minimum_vertices = payload.get("minimum_vertices", 1)
    if keep_largest is not None and (
        not isinstance(keep_largest, int)
        or isinstance(keep_largest, bool)
        or keep_largest < 1
    ):
        raise SystemExit("loose_components keep_largest must be a positive integer")
    if (
        not isinstance(minimum_vertices, int)
        or isinstance(minimum_vertices, bool)
        or minimum_vertices < 1
    ):
        raise SystemExit("loose_components minimum_vertices must be a positive integer")
    return LooseComponentRule(keep_largest=keep_largest, minimum_vertices=minimum_vertices)


def _require_recipe_keys(payload: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise SystemExit(f"Unknown {context} field(s): {', '.join(unknown)}")


def _finite_vector3(value: Any, label: str) -> tuple[float, float, float]:
    if not isinstance(value, list) or len(value) != 3:
        raise SystemExit(f"{label} must contain three numbers")
    result = tuple(_finite_number(item, label) for item in value)
    return (result[0], result[1], result[2])


def _positive_vector3(value: Any, label: str) -> tuple[float, float, float]:
    result = _finite_vector3(value, label)
    if any(item <= 0 for item in result):
        raise SystemExit(f"{label} values must be positive")
    return result


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SystemExit(f"{label} must contain finite numbers")
    result = float(value)
    if not math.isfinite(result):
        raise SystemExit(f"{label} must contain finite numbers")
    return result


def _positive_number(value: Any, label: str) -> float:
    result = _finite_number(value, label)
    if result <= 0:
        raise SystemExit(f"{label} must be positive")
    return result


def point_is_retained(point: tuple[float, float, float], crop: MeshCrop) -> bool:
    offset = tuple(point[index] - crop.center[index] for index in range(3))
    if crop.shape == "box":
        assert crop.size is not None
        inside = all(abs(offset[index]) <= crop.size[index] / 2 for index in range(3))
    else:
        assert crop.radius is not None and crop.height is not None
        inside = math.hypot(offset[0], offset[1]) <= crop.radius and abs(offset[2]) <= crop.height / 2
    return inside if crop.keep == "inside" else not inside


def apply_reversible_cleanup(
    bpy: Any,
    source_objects: list[Any],
    recipe: MeshCleanupRecipe,
) -> tuple[list[Any], dict[str, Any]]:
    try:
        import bmesh
    except ImportError as error:
        raise SystemExit("Mesh cleanup requires Blender's bmesh module") from error

    retained_objects: list[Any] = []
    object_evidence: list[dict[str, Any]] = []
    for source in source_objects:
        retained = source.copy()
        if getattr(source, "data", None) is not None:
            retained.data = source.data.copy()
        retained.name = f"{source.name}_retained"
        bpy.context.collection.objects.link(retained)
        source.name = f"{source.name}_source"
        source.hide_render = True
        source.hide_set(True)
        retained_objects.append(retained)

        if getattr(retained, "type", None) != "MESH":
            continue
        source_vertex_count = len(retained.data.vertices)
        retained_vertex_count = _clean_mesh_object(bmesh, retained, recipe)
        object_evidence.append(
            {
                "object": retained.name,
                "source_vertex_count": source_vertex_count,
                "retained_vertex_count": retained_vertex_count,
                "removed_vertex_count": source_vertex_count - retained_vertex_count,
            }
        )

    source_vertex_count = sum(item["source_vertex_count"] for item in object_evidence)
    retained_vertex_count = sum(item["retained_vertex_count"] for item in object_evidence)
    if source_vertex_count == 0:
        raise SystemExit("Cleanup input does not contain a mesh with vertices")
    if retained_vertex_count == 0:
        raise SystemExit("Cleanup recipe removed every mesh vertex")
    return retained_objects, {
        "schema_version": "1.0",
        "recipe": cleanup_recipe_payload(recipe),
        "source_vertex_count": source_vertex_count,
        "retained_vertex_count": retained_vertex_count,
        "removed_vertex_count": source_vertex_count - retained_vertex_count,
        "retained_ratio": retained_vertex_count / source_vertex_count,
        "source_preserved_in_blend": True,
        "objects": object_evidence,
    }


def _clean_mesh_object(bmesh: Any, obj: Any, recipe: MeshCleanupRecipe) -> int:
    mesh = obj.data
    editable = bmesh.new()
    try:
        editable.from_mesh(mesh)
        if recipe.crop is not None:
            crop = recipe.crop
            discarded = [
                vertex
                for vertex in editable.verts
                if not point_is_retained(_world_point(obj, vertex.co), crop)
            ]
            if discarded:
                bmesh.ops.delete(editable, geom=discarded, context="VERTS")
        if recipe.loose_components is not None:
            _delete_unwanted_components(bmesh, editable, recipe.loose_components)
        editable.to_mesh(mesh)
        mesh.update()
    finally:
        editable.free()

    if recipe.crop is not None:
        invalid = [
            vertex.index
            for vertex in mesh.vertices
            if not point_is_retained(_world_point(obj, vertex.co), recipe.crop)
        ]
        if invalid:
            raise SystemExit(f"Cleanup verification found {len(invalid)} excluded vertices")
    return len(mesh.vertices)


def finalize_cleanup_evidence(
    retained_objects: list[Any],
    recipe: MeshCleanupRecipe,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    retained_by_name = {
        obj.name: obj
        for obj in retained_objects
        if getattr(obj, "type", None) == "MESH"
    }
    retained_total = 0
    for object_evidence in evidence["objects"]:
        obj = retained_by_name.get(object_evidence["object"])
        if obj is None:
            raise SystemExit(f"Retained cleanup object disappeared: {object_evidence['object']}")
        if recipe.crop is not None:
            invalid_count = sum(
                not point_is_retained(_world_point(obj, vertex.co), recipe.crop)
                for vertex in obj.data.vertices
            )
            if invalid_count:
                raise SystemExit(
                    f"Final cleanup verification found {invalid_count} excluded vertices"
                )
        component_sizes = _mesh_component_sizes(obj.data)
        if recipe.loose_components is not None:
            if any(
                size < recipe.loose_components.minimum_vertices
                for size in component_sizes
            ):
                raise SystemExit("Final cleanup verification found an undersized component")
            if (
                recipe.loose_components.keep_largest is not None
                and len(component_sizes) > recipe.loose_components.keep_largest
            ):
                raise SystemExit("Final cleanup verification found too many components")
        retained_count = len(obj.data.vertices)
        if retained_count > object_evidence["source_vertex_count"]:
            raise SystemExit("Final cleanup result gained vertices unexpectedly")
        object_evidence["retained_vertex_count"] = retained_count
        object_evidence["removed_vertex_count"] = (
            object_evidence["source_vertex_count"] - retained_count
        )
        object_evidence["retained_component_count"] = len(component_sizes)
        retained_total += retained_count

    if retained_total == 0:
        raise SystemExit("Final cleanup result contains no mesh vertices")
    source_total = evidence["source_vertex_count"]
    evidence["retained_vertex_count"] = retained_total
    evidence["removed_vertex_count"] = source_total - retained_total
    evidence["retained_ratio"] = retained_total / source_total
    evidence["final_verification_passed"] = True
    return evidence


def _mesh_component_sizes(mesh: Any) -> list[int]:
    neighbors: dict[int, set[int]] = {vertex.index: set() for vertex in mesh.vertices}
    for edge in mesh.edges:
        first, second = edge.vertices
        neighbors[first].add(second)
        neighbors[second].add(first)
    remaining = set(neighbors)
    sizes: list[int] = []
    while remaining:
        seed = remaining.pop()
        size = 1
        stack = [seed]
        while stack:
            current = stack.pop()
            for neighbor in neighbors[current]:
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    size += 1
                    stack.append(neighbor)
        sizes.append(size)
    return sorted(sizes, reverse=True)


def _delete_unwanted_components(bmesh: Any, editable: Any, rule: LooseComponentRule) -> None:
    remaining = set(editable.verts)
    components: list[set[Any]] = []
    while remaining:
        seed = remaining.pop()
        component = {seed}
        stack = [seed]
        while stack:
            vertex = stack.pop()
            for edge in vertex.link_edges:
                neighbor = edge.other_vert(vertex)
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    component.add(neighbor)
                    stack.append(neighbor)
        components.append(component)

    eligible = [component for component in components if len(component) >= rule.minimum_vertices]
    eligible.sort(key=len, reverse=True)
    if rule.keep_largest is not None:
        eligible = eligible[: rule.keep_largest]
    retained = set().union(*eligible) if eligible else set()
    discarded = [vertex for vertex in editable.verts if vertex not in retained]
    if discarded:
        bmesh.ops.delete(editable, geom=discarded, context="VERTS")


def _world_point(obj: Any, coordinate: Any) -> tuple[float, float, float]:
    world = obj.matrix_world @ coordinate
    return (float(world[0]), float(world[1]), float(world[2]))


def cleanup_recipe_payload(recipe: MeshCleanupRecipe) -> dict[str, Any]:
    payload: dict[str, Any] = {"schema_version": recipe.schema_version}
    if recipe.crop is not None:
        crop: dict[str, Any] = {
            "shape": recipe.crop.shape,
            "center": list(recipe.crop.center),
            "keep": recipe.crop.keep,
        }
        if recipe.crop.size is not None:
            crop["size"] = list(recipe.crop.size)
        if recipe.crop.radius is not None:
            crop["radius"] = recipe.crop.radius
        if recipe.crop.height is not None:
            crop["height"] = recipe.crop.height
        payload["crop"] = crop
    if recipe.loose_components is not None:
        payload["loose_components"] = {
            "keep_largest": recipe.loose_components.keep_largest,
            "minimum_vertices": recipe.loose_components.minimum_vertices,
        }
    return payload


def clear_scene(bpy: Any) -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()


def import_asset(bpy: Any, input_path: Path) -> list[Any]:
    suffix = input_path.suffix.lower()
    before = set(bpy.context.scene.objects)

    if suffix == ".obj":
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=str(input_path))
        else:
            bpy.ops.import_scene.obj(filepath=str(input_path))
    elif suffix == ".ply":
        if hasattr(bpy.ops.wm, "ply_import"):
            bpy.ops.wm.ply_import(filepath=str(input_path))
        else:
            bpy.ops.import_mesh.ply(filepath=str(input_path))
    elif suffix in {".glb", ".gltf"}:
        bpy.ops.import_scene.gltf(filepath=str(input_path))
    else:
        raise SystemExit(f"Unsupported input format: {input_path}")

    return [obj for obj in bpy.context.scene.objects if obj not in before]


def normalize_scene_names(bpy: Any, base_name: str) -> None:
    for index, obj in enumerate(bpy.context.scene.objects, start=1):
        obj.name = f"{base_name}_{index:03d}"


def configure_units(bpy: Any, unit_system: str) -> None:
    if unit_system.upper() == "NONE":
        return
    bpy.context.scene.unit_settings.system = unit_system


def apply_scale(bpy: Any, objects: list[Any], scale: float) -> None:
    if scale == 1.0:
        return
    select_only(bpy, objects)
    for obj in objects:
        obj.scale = (obj.scale[0] * scale, obj.scale[1] * scale, obj.scale[2] * scale)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)


def set_origins(bpy: Any, objects: list[Any], origin: str) -> None:
    if origin == "none":
        return
    select_only(bpy, objects)
    if origin == "geometry":
        bpy.ops.object.origin_set(type="ORIGIN_GEOMETRY", center="BOUNDS")
    elif origin == "cursor":
        bpy.ops.object.origin_set(type="ORIGIN_CURSOR")


def apply_decimation(bpy: Any, objects: list[Any], ratio: float) -> None:
    for obj in objects:
        if getattr(obj, "type", None) != "MESH":
            continue
        modifier = obj.modifiers.new(name="scanner_decimate", type="DECIMATE")
        modifier.ratio = ratio
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        bpy.ops.object.modifier_apply(modifier=modifier.name)
        obj.select_set(False)


def relink_textures(bpy: Any, texture_dir: Path) -> None:
    if not texture_dir.is_dir():
        raise SystemExit(f"Texture directory does not exist: {texture_dir}")

    candidates = {
        path.name.lower(): path
        for path in texture_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".exr"}
    }

    for image in bpy.data.images:
        current = Path(image.filepath_from_user() or image.filepath or "")
        replacement = candidates.get(current.name.lower())
        if replacement is not None:
            image.filepath = str(replacement)


def select_only(bpy: Any, objects: list[Any]) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    if objects:
        bpy.context.view_layer.objects.active = objects[0]


if __name__ == "__main__":
    main()
