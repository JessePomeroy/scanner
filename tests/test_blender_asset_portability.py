"""Native Blender checks for assets delivered without their source directory.

Run with: python -B -m unittest discover -s tests -p test_blender_asset_portability.py -v
The checks skip when Blender is unavailable; BLENDER_EXECUTABLE can select it.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PREPARE_SCRIPT = ROOT / "scripts" / "blender" / "prepare_scan_asset.py"


def _prepare_module():
    spec = importlib.util.spec_from_file_location("prepare_scan_asset", PREPARE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_textured_obj(bpy, root: Path) -> Path:
    source = root / "source"
    source.mkdir()
    image = bpy.data.images.new("fixture", width=2, height=2)
    image.pixels[:] = [0.0, 1.0, 0.0, 1.0] * 4
    image.filepath_raw = str(source / "texture.png")
    image.file_format = "PNG"
    image.save()
    bpy.data.images.remove(image)
    (source / "asset.mtl").write_text(
        "newmtl surface\nKd 1 1 1\nmap_Kd texture.png\n", encoding="utf-8"
    )
    obj = source / "asset.obj"
    obj.write_text(
        "mtllib asset.mtl\n"
        "v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\n"
        "vt 0 0\nvt 1 0\nvt 1 1\nvt 0 1\n"
        "usemtl surface\nf 1/1 2/2 3/3 4/4\n",
        encoding="utf-8",
    )
    return obj


def _assert_green_texture(bpy, label: str) -> None:
    meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    assert len(meshes) == 1, f"{label}: expected one mesh"
    images = {
        node.image
        for material in meshes[0].data.materials
        if material is not None and material.node_tree is not None
        for node in material.node_tree.nodes
        if node.type == "TEX_IMAGE" and node.image is not None
    }
    assert len(images) == 1, f"{label}: expected a material texture"
    image = images.pop()
    pixels = list(image.pixels)
    assert tuple(image.size) == (2, 2) and len(pixels) == 16, (
        f"{label}: texture unavailable after relocation: {image.filepath}"
    )
    for index in range(0, len(pixels), 4):
        red, green, blue, alpha = pixels[index : index + 4]
        assert red < 0.01 and green > 0.99 and blue < 0.01 and alpha > 0.99, (
            f"{label}: expected original green texture pixels, got {pixels}"
        )


def _native_case(case: str) -> None:
    import bpy

    module = _prepare_module()
    with tempfile.TemporaryDirectory(prefix="scanner-texture-regression-") as temporary:
        root = Path(temporary)
        obj = _write_textured_obj(bpy, root)
        blend = root / "exports" / "candidates" / "r001" / "asset.blend"
        glb = blend.with_suffix(".glb")
        texture_dir = None
        if case == "relink":
            texture_dir = root / "replacement-textures"
            texture_dir.mkdir()
            (obj.parent / "texture.png").rename(texture_dir / "texture.png")
        if case == "missing":
            (obj.parent / "texture.png").rename(obj.parent / "unavailable.png")
            try:
                module.prepare_asset(
                    module.BlenderAssetOptions(obj, blend, export_glb=glb)
                )
            except SystemExit as error:
                assert "texture" in str(error).lower(), str(error)
                assert not blend.exists() and not glb.exists(), (
                    "Missing texture must fail before publishing outputs"
                )
            else:
                raise AssertionError("Missing texture was silently saved/exported")
            return

        module.prepare_asset(
            module.BlenderAssetOptions(obj, blend, export_glb=glb, texture_dir=texture_dir)
        )
        destination = root / "delivery"
        destination.mkdir()
        delivered_blend = blend.rename(destination / "asset.blend")
        delivered_glb = glb.rename(destination / "asset.glb")
        obj.parent.rename(root / "unavailable-source")
        if texture_dir is not None:
            texture_dir.rename(root / "unavailable-textures")

        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.wm.open_mainfile(filepath=str(delivered_blend))
        _assert_green_texture(bpy, "Reopened .blend")

        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.import_scene.gltf(filepath=str(delivered_glb))
        _assert_green_texture(bpy, "Reimported GLB")

        # An embedded GLB is also a portable recovery source for an old .blend.
        recovered_blend = destination / "recovered.blend"
        module.prepare_asset(module.BlenderAssetOptions(delivered_glb, recovered_blend))
        delivered_glb.rename(destination / "unavailable.glb")
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.wm.open_mainfile(filepath=str(recovered_blend))
        _assert_green_texture(bpy, "Recovered .blend from GLB")


class BlenderAssetPortabilityTests(unittest.TestCase):
    def run_native_case(self, case: str) -> None:
        blender = shutil.which(os.environ.get("BLENDER_EXECUTABLE", "blender"))
        if blender is None:
            self.skipTest("Native Blender is unavailable")
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [
                blender,
                "--background",
                "--factory-startup",
                "--python-exit-code",
                "1",
                "--python",
                str(Path(__file__).resolve()),
                "--",
                case,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=60,
            env=environment,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout)

    def test_blend_and_glb_keep_texture_pixels_without_source_directory(self) -> None:
        self.run_native_case("portable")

    def test_missing_texture_fails_before_saving_outputs(self) -> None:
        self.run_native_case("missing")

    def test_relinked_texture_survives_relocation(self) -> None:
        self.run_native_case("relink")


if __name__ == "__main__":
    if "--" in sys.argv:
        _native_case(sys.argv[sys.argv.index("--") + 1])
    else:
        unittest.main()
