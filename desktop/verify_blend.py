"""Reopen a generated Blender file and verify embedded material images."""
import json
from pathlib import Path
import sys


def main():
    import bpy
    args = sys.argv[sys.argv.index('--') + 1:]
    blend, report = map(Path, args[:2])
    expected = set(args[2:])
    bpy.ops.wm.open_mainfile(filepath=str(blend))
    meshes = [obj for obj in bpy.context.scene.objects if obj.type == 'MESH']
    if not meshes or sum(len(obj.data.polygons) for obj in meshes) == 0:
        raise RuntimeError('The saved Blender file contains no mesh faces.')
    for obj in meshes:
        if not obj.data.uv_layers.active or not obj.data.uv_layers.active.data:
            raise RuntimeError(f'Mesh has no active texture UVs: {obj.name}')
        for material in obj.data.materials:
            if not material or not material.use_nodes:
                raise RuntimeError('Mesh material has no shader nodes.')
            outputs = [node for node in material.node_tree.nodes
                       if node.type == 'OUTPUT_MATERIAL' and node.is_active_output]
            shaders = [link.from_node for node in outputs for link in node.inputs['Surface'].links]
            colors = [link.from_node for node in shaders if node.type == 'BSDF_PRINCIPLED'
                      for link in node.inputs['Base Color'].links]
            if not any(node.type == 'TEX_IMAGE' and node.image is not None for node in colors):
                raise RuntimeError(f'Material texture is not connected to the active shader: {material.name}')
    images = {node.image for obj in meshes for material in obj.data.materials
              if material and material.node_tree for node in material.node_tree.nodes
              if node.type == 'TEX_IMAGE' and node.image is not None}
    actual = set()
    dimensions = {}
    for image in images:
        name = Path(image.filepath).name
        if image.packed_file is None or min(image.size) <= 0 or not image.has_data:
            raise RuntimeError(f'Texture is not embedded or readable: {name}')
        # Accessing a decoded pixel exercises the image buffer after reopening.
        if len(image.pixels) < 4:
            raise RuntimeError(f'Texture pixels unavailable: {name}')
        pixel = image.pixels[0]
        actual.add(name)
        dimensions[name] = list(image.size)
    if not expected or actual != expected:
        raise RuntimeError(f'Embedded textures differ: expected {sorted(expected)}, found {sorted(actual)}')
    evidence = {'verified': True, 'vertices': sum(len(obj.data.vertices) for obj in meshes),
                'faces': sum(len(obj.data.polygons) for obj in meshes), 'textures': dimensions,
                'material_texture_links_verified': True, 'active_uvs_verified': True,
                'visual_quality': 'manual review required'}
    with report.open('x') as stream:
        json.dump(evidence, stream, indent=2)
    print('Embedded textures and mesh verified after reopening.', flush=True)


if __name__ == '__main__':
    main()
