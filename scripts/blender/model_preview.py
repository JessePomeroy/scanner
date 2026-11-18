"""Reopen a delivered Blender file and GLB, check them, and render one preview.

Runs in a fresh Blender process after the source OBJ is gone, so a texture that
was not embedded cannot be hidden by a file that happens to still be on disk.
Nothing is saved back into the checked files.

    blender --background --python model_preview.py -- \
        model.blend model.glb preview.png report.json [--view view.json]
"""

from __future__ import annotations

import json
from pathlib import Path
import sys


PREVIEW_SIZE = 768
DEFAULT_DIRECTION = (0.6, -1.0, -0.35)
DEFAULT_UP = (0.0, 0.0, 1.0)


def mesh_objects(bpy):
    return [obj for obj in bpy.context.scene.objects if obj.type == "MESH" and len(obj.data.polygons)]


def texture_images(meshes):
    return {
        node.image
        for obj in meshes for material in obj.data.materials
        if material and material.use_nodes
        for node in material.node_tree.nodes
        if node.type == "TEX_IMAGE" and node.image is not None
    }


def check_scene(bpy, label: str, *, require_packed: bool) -> dict[str, object]:
    meshes = mesh_objects(bpy)
    if not meshes:
        raise ValueError(f"{label}: no mesh faces")
    for obj in meshes:
        if not obj.data.uv_layers.active:
            raise ValueError(f"{label}: mesh has no texture coordinates")
    images = texture_images(meshes)
    if not images:
        raise ValueError(f"{label}: no texture is connected to the mesh")
    for image in images:
        # Reading a pixel proves the embedded image decodes after reopening.
        if min(image.size) <= 0 or not image.has_data or len(image.pixels) < 4:
            raise ValueError(f"{label}: texture is unreadable")
        if require_packed and image.packed_file is None:
            raise ValueError(f"{label}: texture is not embedded")
    return {
        "meshes": len(meshes),
        "faces": sum(len(obj.data.polygons) for obj in meshes),
        "textures": sorted(list(image.size) for image in images),
    }


def framing(meshes):
    """Center and size from the 1st-99th percentile so stray specks do not shrink the subject."""
    import numpy as np
    from mathutils import Vector

    coordinates = np.concatenate([
        np.array([tuple(obj.matrix_world @ vertex.co) for vertex in obj.data.vertices])
        for obj in meshes
    ])
    low = Vector(np.quantile(coordinates, 0.01, axis=0))
    high = Vector(np.quantile(coordinates, 0.99, axis=0))
    diagonal = (high - low).length
    if not diagonal > 0:
        raise ValueError("preview: mesh has no extent")
    return (low + high) / 2, diagonal


def render_preview(bpy, destination: Path, view: dict[str, object] | None) -> dict[str, object]:
    from mathutils import Matrix, Vector

    meshes = mesh_objects(bpy)
    center, diagonal = framing(meshes)
    if view is not None:
        direction = center - Vector(view["location"])
        up = Vector(view["up"])
    else:
        direction, up = Vector(DEFAULT_DIRECTION), Vector(DEFAULT_UP)
    if direction.length == 0:
        direction = Vector(DEFAULT_DIRECTION)
    direction.normalize()
    right = direction.cross(up)
    if right.length < 1e-6:
        right = direction.cross(Vector((1.0, 0.0, 0.0)))
    right.normalize()
    up = right.cross(direction).normalized()

    # Photo textures already hold the scene's light; show them as captured.
    for material in bpy.data.materials:
        if not material.use_nodes:
            continue
        tree = material.node_tree
        texture = next((node for node in tree.nodes if node.type == "TEX_IMAGE" and node.image), None)
        terminal = next((node for node in tree.nodes if node.type == "OUTPUT_MATERIAL"), None)
        if texture and terminal:
            emission = tree.nodes.new("ShaderNodeEmission")
            tree.links.new(texture.outputs["Color"], emission.inputs["Color"])
            tree.links.new(emission.outputs[0], terminal.inputs["Surface"])

    scene = bpy.context.scene
    camera_data = bpy.data.cameras.new("Preview")
    camera = bpy.data.objects.new("Preview", camera_data)
    scene.collection.objects.link(camera)
    # A 50 mm lens sees about 0.36 x distance to each side; 1.45 x the diagonal
    # keeps the whole bounding sphere in frame.
    camera.location = center - direction * diagonal * 1.45
    camera.rotation_euler = Matrix((right, up, -direction)).transposed().to_euler()
    camera_data.lens = 50
    camera_data.clip_start = diagonal * 0.001
    camera_data.clip_end = diagonal * 20
    scene.camera = camera
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 4
    scene.cycles.use_denoising = False
    scene.render.threads_mode = "FIXED"
    scene.render.threads = 4
    scene.render.resolution_x = scene.render.resolution_y = PREVIEW_SIZE
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = True
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"
    scene.view_settings.exposure = 0
    scene.view_settings.gamma = 1
    scene.render.filepath = str(destination)
    bpy.ops.render.render(write_still=True)
    if not destination.is_file():
        raise ValueError("preview: render produced no image")
    return {"size": PREVIEW_SIZE, "view": "capture_photo" if view is not None else "default"}


def main() -> None:
    import bpy

    arguments = sys.argv[sys.argv.index("--") + 1:]
    view_path = None
    if "--view" in arguments:
        index = arguments.index("--view")
        view_path = Path(arguments[index + 1])
        del arguments[index:index + 2]
    blend, glb, preview, report = map(Path, arguments)
    for path in (preview, report):
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"Delivery never replaces existing files: {path}")
    view = json.loads(view_path.read_text()) if view_path is not None else None

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(glb))
    results = {"glb": check_scene(bpy, "glb", require_packed=False)}
    bpy.ops.wm.open_mainfile(filepath=str(blend))
    results["blend"] = check_scene(bpy, "blend", require_packed=True)
    results["preview"] = render_preview(bpy, preview, view)
    with report.open("x") as stream:
        json.dump({"status": "passed", "assets": results}, stream, indent=2)
    print("SCANNER_STAGE: Blender files verified and preview rendered", flush=True)


if __name__ == "__main__":
    main()
