from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tests"))
from app.heavy_work import heavy_work, native_kwargs
from app.retro_export import export_retro_asset, inspect_retro_glb, sha256
from app.retro_style import RetroStyle, quantize_linear_pixels
from heavy_work_fixture import isolated_heavy_work


def textured_triangle(root: Path, name: str = "source") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (150, 100, 50)).save(root / f"{name}.png")
    (root / f"{name}.mtl").write_text(f"newmtl surface\nmap_Kd {name}.png\n")
    obj = root / f"{name}.obj"
    obj.write_text(f"mtllib {name}.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\n"
                   "vt 0 0\nvt 1 0\nvt 0 1\nusemtl surface\nf 1/1 2/2 3/3\n")
    return obj


def glb_contract_fixture(path: Path, *, nearest: bool = True, unlit: bool = True) -> None:
    document = {"asset": {"version": "2.0"}, "materials": [{"extensions": {"KHR_materials_unlit": {}} if unlit else {}}],
                "images": [{"bufferView": 0}], "buffers": [{}],
                "samplers": [{"magFilter": 9728 if nearest else 9729, "minFilter": 9728}]}
    content = json.dumps(document).encode()
    content += b" " * ((-len(content)) % 4)
    path.write_bytes(struct.pack("<4sIIII", b"glTF", 2, 20 + len(content), len(content), 0x4E4F534A) + content)


class RetroStyleTests(unittest.TestCase):
    def test_bounded_style_options_reject_ambiguous_or_unbounded_values(self):
        for kwargs in ({"triangle_budget": 999}, {"triangle_budget": True}, {"texture_size": 8192}, {"dither": "yes"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                RetroStyle(**kwargs)
        self.assertEqual(RetroStyle().triangle_budget, 500)

    def test_export_commands_default_to_500_and_keep_explicit_budgets(self):
        def load_script(name, path):
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            return module

        cli = load_script("retro_cli_defaults", ROOT / "scripts/export_retro_asset.py")
        prepare = load_script("retro_prepare_defaults", ROOT / "scripts/blender/prepare_scan_asset.py")
        for budget in (500, 1000, 2000):
            cli_options = [] if budget == 500 else ["--triangles", str(budget)]
            with patch.object(sys, "argv", ["export_retro_asset.py", "source.obj", "new-output", *cli_options]), \
                    patch.object(cli, "export_retro_asset", return_value={}) as export, patch("builtins.print"):
                cli.main()
            self.assertEqual(export.call_args.args[2].triangle_budget, budget)
            native_options = [] if budget == 500 else ["--triangle-budget", str(budget)]
            options = prepare.parse_blender_args([
                "source.obj", "retro.blend", "--asset-style", "retro",
                "--export-glb", "retro.glb", "--retro-report", "report.json", *native_options,
            ])
            self.assertEqual(options.triangle_budget, budget)
        self.assertEqual(prepare.BlenderAssetOptions(Path("source.obj"), Path("asset.blend")).triangle_budget, 500)
        self.assertEqual(prepare.parse_blender_args(["source.obj", "asset.blend"]).asset_style, "standard")

    def test_color_reduction_is_in_srgb_and_preserves_alpha(self):
        values = np.array([[[.21404114, .0031308, 1, .25], [0, .5, .8, 1]]], dtype=np.float32)
        result = quantize_linear_pixels(values)
        srgb = np.where(result[:, :, :3] <= .0031308, result[:, :, :3] * 12.92,
                        1.055 * result[:, :, :3] ** (1 / 2.4) - .055)
        np.testing.assert_allclose(srgb * 31, np.round(srgb * 31), atol=1e-5)
        self.assertAlmostEqual(float(srgb[0, 0, 0]), 16 / 31, places=5)
        np.testing.assert_array_equal(result[:, :, 3], values[:, :, 3])
        np.testing.assert_array_equal(quantize_linear_pixels(values), result)

    def test_dithering_is_repeatable_and_does_not_modify_the_input(self):
        values = np.full((8, 8, 4), .21404114, dtype=np.float32)
        values[:, :, 3] = 1
        original = values.copy()
        result = quantize_linear_pixels(values, dither=True)
        np.testing.assert_array_equal(result, quantize_linear_pixels(values, dither=True))
        np.testing.assert_array_equal(values, original)
        self.assertEqual(len(np.unique(result[:, :, 0])), 2)
        with self.assertRaises(ValueError):
            quantize_linear_pixels(np.full((2, 2, 4), np.nan))


class RetroExportTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_heavy_work())
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source = textured_triangle(self.root / "input")
        self.before = {path.name: path.read_bytes() for path in self.source.parent.iterdir()}
        self.enterContext(patch("app.retro_export.shutil.which", return_value="/test/blender"))

    def worker(self, command, **kwargs):
        arguments = command[command.index("--") + 1:]
        if Path(command[command.index("--python") + 1]).name == "prepare_scan_asset.py":
            source, blend = map(Path, arguments[:2])
            self.assertNotEqual(source, self.source)
            self.assertFalse((source.parent / "neighbor-private.txt").exists())
            self.assertEqual(source.read_bytes(), self.source.read_bytes())
            textured_triangle(blend.parent, "retro")
            blend.write_bytes(b"mock Blender export; real format tested by native fixture")
            glb_contract_fixture(blend.with_suffix(".glb"))
            (blend.parent / "retro-report.json").write_text(json.dumps({"triangles": 1}))
        else:
            Path(arguments[2]).write_text(json.dumps({"status": "passed"}))
        self.assertGreater(kwargs["timeout"], 0)
        return SimpleNamespace(returncode=0)

    def test_derived_export_preserves_originals_bundles_dependencies_and_records_verification(self):
        (self.source.parent / "neighbor-private.txt").write_text("not an asset dependency")
        with patch("app.retro_export.subprocess.run", side_effect=self.worker) as worker:
            outputs = export_retro_asset(self.source, self.root / "retro", RetroStyle(500, 128))
        self.assertEqual(worker.call_count, 2)
        for name, content in self.before.items():
            self.assertEqual((self.source.parent / name).read_bytes(), content)
        report = json.loads(Path(outputs["retro_export_report"]).read_text())
        self.assertEqual(report["state"], "succeeded")
        self.assertTrue(report["originals_unchanged"])
        self.assertEqual(report["settings"]["triangle_budget"], 500)
        self.assertFalse(any(path.name.startswith(".source-") for path in (self.root / "retro").iterdir()))
        with zipfile.ZipFile(outputs["retro_bundle"]) as archive:
            self.assertEqual(set(archive.namelist()), {"retro.obj", "retro.mtl", "retro.png", "retro-report.json", "texture-quality.json"})

    def test_existing_destination_and_source_ancestor_are_never_overwritten(self):
        output = self.root / "existing"
        output.mkdir()
        (output / "keep.txt").write_text("keep")
        with patch("app.retro_export.subprocess.run") as worker:
            for destination in (output, self.root, self.source.parent):
                with self.subTest(destination=destination), self.assertRaises((FileExistsError, ValueError)):
                    export_retro_asset(self.source, destination)
        worker.assert_not_called()
        self.assertEqual((output / "keep.txt").read_text(), "keep")

    def test_worker_failure_and_timeout_keep_diagnostics_without_claiming_success(self):
        for index, failure in enumerate((SimpleNamespace(returncode=1), subprocess.TimeoutExpired("blender", 10))):
            with self.subTest(failure=failure):
                destination = self.root / f"failed-{index}"
                with patch("app.retro_export.subprocess.run", side_effect=failure if isinstance(failure, Exception) else None,
                           return_value=failure), self.assertRaises((RuntimeError, TimeoutError)):
                    export_retro_asset(self.source, destination)
                self.assertEqual(json.loads((destination / "export.json").read_text())["state"], "failed")
                self.assertTrue((destination / "prepare.log").is_file())
                self.assertFalse((destination / "retro-obj.zip").exists())
        self.assertEqual(self.source.read_bytes(), self.before[self.source.name])

    def test_unsafe_material_dependency_is_rejected_before_blender(self):
        self.source.with_suffix(".mtl").write_text("newmtl surface\nmap_Kd ../neighbor-private.txt\n")
        with patch("app.retro_export.subprocess.run") as worker, self.assertRaises(ValueError):
            export_retro_asset(self.source, self.root / "unsafe")
        worker.assert_not_called()

    def test_glb_must_preserve_pixelated_unlit_material_semantics(self):
        path = self.root / "asset.glb"
        for kwargs in ({"nearest": False}, {"unlit": False}):
            glb_contract_fixture(path, **kwargs)
            with self.assertRaises(ValueError):
                inspect_retro_glb(path)
        path.write_bytes(b"bad")
        with self.assertRaises(ValueError):
            inspect_retro_glb(path)


def native_fixture(directory: Path) -> None:
    import bpy

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24)
    obj = bpy.context.object
    image = bpy.data.images.new("source", width=32, height=32)
    image.pixels[:] = [.8, .3, .1, 1] * (32 * 32)
    image.filepath_raw = str(directory / "color.png")
    image.file_format = "PNG"
    image.save()
    material = bpy.data.materials.new("source")
    material.use_nodes = True
    texture = material.node_tree.nodes.new("ShaderNodeTexImage")
    texture.image = image
    shader = material.node_tree.nodes.get("Principled BSDF")
    material.node_tree.links.new(texture.outputs["Color"], shader.inputs["Base Color"])
    obj.data.materials.append(material)
    bpy.ops.wm.obj_export(filepath=str(directory / "sphere.obj"), forward_axis="Y", up_axis="Z", path_mode="RELATIVE")


@unittest.skipUnless(os.environ.get("SCANNER_TEST_NATIVE_RETRO") == "1", "Set SCANNER_TEST_NATIVE_RETRO=1 for native Blender bake/reopen verification")
class NativeRetroTests(unittest.TestCase):
    def test_color_bake_budget_dither_and_portable_outputs(self):
        blender = shutil.which("blender")
        if not blender:
            self.skipTest("Blender is unavailable")
        with tempfile.TemporaryDirectory() as temporary, heavy_work("Native Retro regression fixture"):
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            subprocess.run([blender, "--background", "--factory-startup", "--disable-autoexec", "--threads", "4",
                            "--python-exit-code", "1", "--python", str(Path(__file__).resolve()), "--", str(source)],
                           check=True, timeout=60, **native_kwargs())
            hashes = {path.name: sha256(path) for path in source.iterdir()}
            outputs = export_retro_asset(source / "sphere.obj", root / "retro", RetroStyle(500, 128, True))
            report = json.loads(Path(outputs["retro_report"]).read_text())
            self.assertLessEqual(report["triangles"], 500)
            self.assertGreater(report["triangles"], 450)
            self.assertEqual({path.name: sha256(path) for path in source.iterdir()}, hashes)
            with Image.open(source / "color.png") as original, Image.open(root / "retro/retro.png") as baked:
                self.assertEqual(baked.size, (128, 128))
                colors = np.asarray(baked.convert("RGB"), dtype=np.int16)
                populated = colors.max(axis=2) > 10
                self.assertGreater(float(populated.mean()), .15)
                expected = np.array(original.convert("RGB").getpixel((0, 0)), dtype=np.int16)
                self.assertLessEqual(int(np.abs(np.median(colors[populated], axis=0) - expected).max()), 9)


if __name__ == "__main__":
    if "--" in sys.argv:
        native_fixture(Path(sys.argv[sys.argv.index("--") + 1]))
    else:
        unittest.main()
