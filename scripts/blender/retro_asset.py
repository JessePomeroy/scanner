"""Native Blender implementation and independent reopen checks for Retro exports."""

from __future__ import annotations

from dataclasses import asdict
import json
import math
from pathlib import Path
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from app.retro_style import RetroStyle, quantize_linear_pixels


def triangle_count(obj) -> int:
    obj.data.calc_loop_triangles()
    return len(obj.data.loop_triangles)


def select(bpy, objects, active=None) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = active or objects[-1]


def prepare_retro_asset(bpy, objects, blend: Path, glb: Path, report_path: Path, style: RetroStyle) -> None:
    import numpy as np
    from mathutils import Vector

    started = perf_counter()
    paths = [blend, glb, report_path, blend.with_suffix(".png"),
             blend.with_suffix(".obj"), blend.with_suffix(".mtl")]
    if len(set(path.absolute() for path in paths)) != len(paths):
        raise ValueError("Retro output paths must be distinct")
    for path in paths:
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"Retro exports never replace existing files: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
    sources = [obj for obj in objects if obj.type == "MESH" and len(obj.data.polygons)]
    original_triangles = sum(triangle_count(obj) for obj in sources)
    if not 1 <= original_triangles <= 3_000_000:
        raise ValueError("Retro source must contain 1–3,000,000 triangles")
    for obj in sources:
        if not obj.data.uv_layers.active or not obj.data.materials:
            raise ValueError("Retro export requires a textured mesh with UV coordinates")
        for index in {polygon.material_index for polygon in obj.data.polygons}:
            material = obj.data.materials[index]
            if not material or not material.use_nodes:
                raise ValueError("Retro source materials must contain readable image textures")
            images = [node.image for node in material.node_tree.nodes
                      if node.type == "TEX_IMAGE" and node.image]
            # Packed images can still have an unloaded pixel buffer after OBJ import.
            if not images or any(len(image.pixels) < 4 for image in images):
                raise ValueError("Retro source materials must contain readable image textures")

    print("SCANNER_STAGE: Simplifying Retro silhouette", flush=True)
    copies = []
    for source in sources:
        copy = source.copy()
        copy.data = source.data.copy()
        bpy.context.collection.objects.link(copy)
        copies.append(copy)
    select(bpy, copies)
    if len(copies) > 1:
        bpy.ops.object.join()
    low = bpy.context.view_layer.objects.active
    low.name = "Scanner_Retro"
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    if original_triangles > style.triangle_budget:
        modifier = low.modifiers.new("Retro triangle budget", "DECIMATE")
        modifier.ratio = style.triangle_budget / original_triangles
        modifier.use_collapse_triangulate = True
        bpy.ops.object.modifier_apply(modifier=modifier.name)
    triangulate = low.modifiers.new("Explicit triangles", "TRIANGULATE")
    bpy.ops.object.modifier_apply(modifier=triangulate.name)
    actual_triangles = triangle_count(low)
    if not 0 < actual_triangles <= style.triangle_budget:
        raise ValueError(f"Unable to meet Retro triangle budget: {actual_triangles}")

    print("SCANNER_STAGE: Unwrapping and baking Retro texture", flush=True)
    select(bpy, [low])
    for layer in list(low.data.uv_layers):
        low.data.uv_layers.remove(layer)
    low.data.uv_layers.new(name="RetroUV")
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.uv.smart_project(angle_limit=math.radians(66), island_margin=4 / style.texture_size)
    bpy.ops.object.mode_set(mode="OBJECT")
    # Smooth projection normals prevent artificial ray gaps at the new coarse edges.
    for polygon in low.data.polygons:
        polygon.use_smooth = True
    material = bpy.data.materials.new("Retro_Photo_Color")
    material.use_nodes = True
    low.data.materials.clear()
    low.data.materials.append(material)
    for polygon in low.data.polygons:
        polygon.material_index = 0
    image = bpy.data.images.new("Retro_Atlas", width=style.texture_size, height=style.texture_size,
                                alpha=True, float_buffer=True)
    image.colorspace_settings.name = "sRGB"
    tree = material.node_tree
    texture = tree.nodes.new("ShaderNodeTexImage")
    texture.image = image
    texture.interpolation = "Closest"
    tree.nodes.active = texture
    shader = next(node for node in tree.nodes if node.type == "BSDF_PRINCIPLED")
    shader.inputs["Roughness"].default_value = 1
    coordinates = [low.matrix_world @ Vector(corner) for corner in low.bound_box]
    diagonal = (Vector(tuple(max(point[axis] for point in coordinates) for axis in range(3))) -
                Vector(tuple(min(point[axis] for point in coordinates) for axis in range(3)))).length
    if not math.isfinite(diagonal) or diagonal <= 0:
        raise ValueError("Retro source has degenerate bounds")
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 8
    scene.render.threads_mode = "FIXED"
    scene.render.threads = 4
    select(bpy, [*sources, low], low)
    bpy.ops.object.bake(type="DIFFUSE", pass_filter={"COLOR"}, use_selected_to_active=True,
                        use_clear=True, margin=2, cage_extrusion=diagonal * .03,
                        max_ray_distance=diagonal * .08)
    pixels = np.empty(style.texture_size * style.texture_size * 4, dtype=np.float32)
    image.pixels.foreach_get(pixels)
    pixels = pixels.reshape(style.texture_size, style.texture_size, 4)
    if not np.isfinite(pixels).all() or not np.any(pixels[:, :, 3] > 0):
        raise ValueError("Retro bake produced an invalid or uncovered texture")
    image.pixels.foreach_set(quantize_linear_pixels(pixels, dither=style.dither).ravel())
    image.update()
    image.filepath_raw = str(blend.with_suffix(".png"))
    image.file_format = "PNG"
    image.save()
    tree.links.new(texture.outputs["Color"], shader.inputs["Base Color"])

    for source in sources:
        bpy.data.objects.remove(source, do_unlink=True)
    for collection in (bpy.data.meshes, bpy.data.materials, bpy.data.images):
        for item in list(collection):
            if item.users == 0 and item != image:
                collection.remove(item)
    for polygon in low.data.polygons:
        polygon.use_smooth = False
    select(bpy, [low])
    # OBJ carries portable color/UVs; filtering and unlit shading belong to its viewer.
    bpy.ops.wm.obj_export(filepath=str(blend.with_suffix(".obj")), export_selected_objects=True,
                          forward_axis="Y", up_axis="Z", path_mode="RELATIVE")
    output = next(node for node in tree.nodes if node.type == "OUTPUT_MATERIAL")
    emission = tree.nodes.new("ShaderNodeEmission")
    tree.links.new(texture.outputs["Color"], emission.inputs["Color"])
    # This is the glTF exporter's supported shadeless graph, not an emissive
    # PBR surface that would respond differently to a game engine's lighting.
    light_path = tree.nodes.new("ShaderNodeLightPath")
    transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
    mix = tree.nodes.new("ShaderNodeMixShader")
    tree.links.new(light_path.outputs["Is Camera Ray"], mix.inputs[0])
    tree.links.new(transparent.outputs[0], mix.inputs[1])
    tree.links.new(emission.outputs[0], mix.inputs[2])
    tree.links.new(mix.outputs[0], output.inputs["Surface"])
    tree.nodes.remove(shader)
    image.pack()
    image.filepath = "//" + blend.with_suffix(".png").name
    scene.view_settings.view_transform = "Standard"
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.spaces.active.shading.type = "MATERIAL"
    print("SCANNER_STAGE: Saving portable Retro assets", flush=True)
    bpy.ops.export_scene.gltf(filepath=str(glb), export_format="GLB", use_selection=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(blend))
    report = {
        "schema_version": "1.0", "style": "retro", "settings": asdict(style),
        "source_triangles": original_triangles, "triangles": actual_triangles,
        "vertices": len(low.data.vertices), "atlas_size": [style.texture_size] * 2,
        "color_bits_per_channel": 5, "texture_filter": "nearest", "shading": "unlit",
        "uvs": "New atlas; source photo color baked from high mesh onto simplified copy",
        "cage_extrusion_relative_to_diagonal": .03, "max_ray_distance_relative_to_diagonal": .08,
        "elapsed_seconds": perf_counter() - started, "blender_version": bpy.app.version_string,
        "visual_quality": "Requires visual inspection; this is a style preset, not PS1 hardware emulation",
    }
    with report_path.open("x") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)


def verify_retro_files(blend: Path, glb: Path, report: Path, style: RetroStyle) -> None:
    import bpy

    results = {}
    for label, path in (("blend", blend), ("glb", glb)):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        if label == "blend":
            bpy.ops.wm.open_mainfile(filepath=str(path))
        else:
            bpy.ops.import_scene.gltf(filepath=str(path))
        meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
        if len(meshes) != 1 or not 0 < triangle_count(meshes[0]) <= style.triangle_budget:
            raise ValueError(f"{label}: invalid Retro geometry")
        mesh = meshes[0]
        if not mesh.data.uv_layers.active:
            raise ValueError(f"{label}: missing Retro UVs")
        textures = [node for material in mesh.data.materials if material and material.use_nodes
                    for node in material.node_tree.nodes if node.type == "TEX_IMAGE" and node.image]
        if len(textures) != 1:
            raise ValueError(f"{label}: expected one baked texture")
        node = textures[0]
        image = node.image
        if tuple(image.size) != (style.texture_size, style.texture_size) or not image.has_data or not image.packed_file:
            raise ValueError(f"{label}: texture not embedded or wrong size")
        if node.interpolation != "Closest" or len(image.pixels) < 4:
            raise ValueError(f"{label}: pixelated texture sampling was not preserved")
        results[label] = {"triangles": triangle_count(mesh), "texture_size": list(image.size),
                          "embedded_texture": True, "nearest_filter": True}
    with report.open("x") as stream:
        json.dump({"status": "passed", "assets": results}, stream, indent=2)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("blend", type=Path)
    parser.add_argument("glb", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--triangle-budget", type=int, default=500)
    parser.add_argument("--texture-size", type=int, default=256)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    verify_retro_files(args.blend, args.glb, args.report, RetroStyle(args.triangle_budget, args.texture_size))
