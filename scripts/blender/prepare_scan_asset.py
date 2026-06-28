"""Import reconstruction output into Blender and save a .blend scene.

Run with Blender:

blender --background --python scripts/blender/prepare_scan_asset.py -- input.obj output.blend
"""

from __future__ import annotations

from pathlib import Path
import sys

import bpy


def main() -> None:
    args = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    if len(args) != 2:
        raise SystemExit("Usage: blender --background --python prepare_scan_asset.py -- input.(obj|ply) output.blend")

    input_path = Path(args[0])
    output_path = Path(args[1])
    output_path.parent.mkdir(parents=True, exist_ok=True)

    clear_scene()
    import_asset(input_path)
    normalize_scene_names(input_path.stem)
    bpy.ops.wm.save_as_mainfile(filepath=str(output_path))


def clear_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()


def import_asset(input_path: Path) -> None:
    suffix = input_path.suffix.lower()
    if suffix == ".obj":
        bpy.ops.wm.obj_import(filepath=str(input_path))
    elif suffix == ".ply":
        bpy.ops.wm.ply_import(filepath=str(input_path))
    else:
        raise SystemExit(f"Unsupported input format: {input_path}")


def normalize_scene_names(base_name: str) -> None:
    for index, obj in enumerate(bpy.context.scene.objects, start=1):
        obj.name = f"{base_name}_{index:03d}"


if __name__ == "__main__":
    main()
