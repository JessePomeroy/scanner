"""Import reconstruction output into Blender and save a prepared scene.

Run with Blender:

blender --background --python scripts/blender/prepare_scan_asset.py -- \
  input.(obj|ply|glb|gltf) output.blend
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
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

    return BlenderAssetOptions(
        input_path=parsed.input,
        output_path=parsed.output,
        scale=parsed.scale,
        decimate_ratio=parsed.decimate_ratio,
        texture_dir=parsed.texture_dir,
        export_glb=parsed.export_glb,
        origin=parsed.origin,
        set_units=parsed.set_units.upper(),
    )


def prepare_asset(options: BlenderAssetOptions) -> None:
    try:
        import bpy
    except ImportError as error:
        raise SystemExit("This script must be run with Blender's Python runtime.") from error

    options.output_path.parent.mkdir(parents=True, exist_ok=True)
    if options.export_glb is not None:
        options.export_glb.parent.mkdir(parents=True, exist_ok=True)

    clear_scene(bpy)
    imported_objects = import_asset(bpy, options.input_path)
    normalize_scene_names(bpy, options.input_path.stem)
    configure_units(bpy, options.set_units)
    apply_scale(bpy, imported_objects, options.scale)
    set_origins(bpy, imported_objects, options.origin)
    if options.decimate_ratio is not None:
        apply_decimation(bpy, imported_objects, options.decimate_ratio)
    if options.texture_dir is not None:
        relink_textures(bpy, options.texture_dir)

    bpy.ops.wm.save_as_mainfile(filepath=str(options.output_path))
    if options.export_glb is not None:
        bpy.ops.export_scene.gltf(filepath=str(options.export_glb), export_format="GLB")


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
