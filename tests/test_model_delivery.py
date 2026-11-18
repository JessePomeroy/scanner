from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))
from app.artifacts import discover_standard_output_paths
from app.jobs import JobStore
from app.mask_generator import generate_mask_proposals
from app.model_delivery import export_blender_model, export_camera_view, read_camera_view
from app.model_library import library_name, publish_to_library
from app.object_pipeline import OBJECT_STEPS, reconstruct_object, step_message
from app.sam2_generator import Sam2Runtime
from app.scan_metadata import load_scan_metadata
from app.scan_package import validate_and_report_scan
from heavy_work_fixture import isolated_heavy_work
import notify_reconstruction_jobs as notifier
from test_sam2_generator import fake_worker, single_box_scan


def textured_obj(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (160, 120, 80)).save(directory / "texture.jpg")
    (directory / "scene_textured.mtl").write_text("newmtl surface\nmap_Kd texture.jpg\n")
    mesh = directory / "scene_textured.obj"
    mesh.write_text(
        "mtllib scene_textured.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nvt 0 0\nvt 1 0\nvt 0 1\n"
        "usemtl surface\nf 1/1 2/2 3/3\n"
    )
    return mesh


FAKE_BLENDER = """#!/usr/bin/env python3
import json, sys
from pathlib import Path
args = sys.argv[sys.argv.index("--") + 1:]
script = Path(sys.argv[sys.argv.index("--python") + 1]).name
mode = Path(__file__).with_suffix(".mode").read_text().strip()
if script == "prepare_scan_asset.py":
    assert Path(args[0]).is_file(), "private source copy missing"
    if mode == "prepare-fails":
        sys.exit(3)
    Path(args[1]).write_bytes(b"BLENDER")
    Path(args[args.index("--export-glb") + 1]).write_bytes(b"glTF")
else:
    blend, glb, preview, report = map(Path, args[:4])
    view = json.loads(Path(args[args.index("--view") + 1]).read_text()) if "--view" in args else None
    preview.write_bytes(b"PNG")
    report.write_text(json.dumps({"status": "failed" if mode == "verify-fails" else "passed", "view": view}))
"""


class CameraViewTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "images.txt"

    def write(self, *rows):
        self.path.write_text("# Image list\n# IMAGE_ID, QW, QX, QY, QZ, TX, TY, TZ, CAMERA_ID, NAME\n" + "\n".join(rows) + "\n")

    def test_identity_pose_looks_down_plus_z_with_image_up_along_minus_y(self):
        self.write("1 1 0 0 0 1 2 3 1 frame_000000.jpg", "10 20 -1")
        view = read_camera_view(self.path, "frame_000000.jpg")
        self.assertEqual(view["image"], "frame_000000.jpg")
        self.assertEqual(view["location"], [-1.0, -2.0, -3.0])
        self.assertEqual(view["up"], [0.0, -1.0, 0.0])

    def test_rotated_pose_uses_the_transposed_rotation(self):
        # 90 degrees about Z: world-to-camera maps world X to camera Y.
        half = 0.5 ** 0.5
        self.write(f"7 {half} 0 0 {half} 0 5 0 1 a.jpg", "")
        view = read_camera_view(self.path, "a.jpg")
        for actual, expected in zip(view["location"], (-5.0, 0.0, 0.0)):
            self.assertAlmostEqual(actual, expected)
        for actual, expected in zip(view["up"], (-1.0, 0.0, 0.0)):
            self.assertAlmostEqual(actual, expected)

    def test_prefers_the_boxed_photo_and_falls_back_to_the_first_aligned_one(self):
        self.write("1 1 0 0 0 1 0 0 1 a.jpg", "", "2 1 0 0 0 2 0 0 1 b.jpg", "1 2 3", "3 1 0 0 0 3 0 0 1 c.jpg", "")
        self.assertEqual(read_camera_view(self.path, "b.jpg")["location"][0], -2.0)
        fallback = read_camera_view(self.path, "never-aligned.jpg")
        self.assertEqual((fallback["image"], fallback["location"][0]), ("a.jpg", -1.0))

    def test_unusable_models_give_no_view_instead_of_a_wrong_one(self):
        for rows in ((), ("1 1 0 0 0 1 2 3 1",), ("1 x 0 0 0 1 2 3 1 a.jpg", ""),
                     ("1 0 0 0 0 1 2 3 1 a.jpg", ""), ("1 nan 0 0 0 1 2 3 1 a.jpg", "")):
            with self.subTest(rows=rows):
                self.write(*rows)
                self.assertIsNone(read_camera_view(self.path, "a.jpg"))
        self.assertIsNone(read_camera_view(self.root / "missing.txt", "a.jpg"))

    def test_camera_export_failure_is_only_a_missing_view(self):
        failed = SimpleNamespace(returncode=1)
        with patch("app.model_delivery.subprocess.run", return_value=failed):
            self.assertIsNone(export_camera_view("colmap", self.root / "sparse", self.root / "one", "a.jpg"))
        with patch("app.model_delivery.subprocess.run", side_effect=OSError("no colmap")):
            self.assertIsNone(export_camera_view("colmap", self.root / "sparse", self.root / "two", "a.jpg"))

        def exported(command, **kwargs):
            (Path(command[command.index("--output_path") + 1]) / "images.txt").write_text(
                "1 1 0 0 0 4 0 0 1 a.jpg\n\n")
            return SimpleNamespace(returncode=0)

        with patch("app.model_delivery.subprocess.run", side_effect=exported):
            view = export_camera_view("colmap", self.root / "sparse", self.root / "three", "a.jpg")
        self.assertEqual(view["location"], [-4.0, 0.0, 0.0])


class ModelDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_heavy_work())
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.mesh = textured_obj(self.root / "dense")
        self.blender = self.root / "tools/blender"
        self.blender.parent.mkdir()
        self.blender.write_text(FAKE_BLENDER)
        self.blender.chmod(self.blender.stat().st_mode | stat.S_IXUSR)
        self.mode = self.blender.with_suffix(".mode")
        self.mode.write_text("ok")

    def export(self, **options):
        return export_blender_model(self.mesh, self.root / "exports/model", blender=str(self.blender), **options)

    def test_publishes_reopened_files_and_passes_the_capture_view_to_the_preview(self):
        before = {path.name: path.read_bytes() for path in self.mesh.parent.iterdir()}
        view = {"image": "a.jpg", "location": [1.0, 2.0, 3.0], "up": [0.0, 0.0, 1.0]}
        outputs = self.export(view=view)
        self.assertEqual(set(outputs), {"model_blend", "model_glb", "model_preview",
                                        "model_verification", "model_export_report"})
        for path in outputs.values():
            self.assertTrue(Path(path).is_file())
        self.assertEqual(json.loads(Path(outputs["model_verification"]).read_text())["view"], view)
        state = json.loads(Path(outputs["model_export_report"]).read_text())
        self.assertEqual((state["state"], [item["stage"] for item in state["commands"]]),
                         ("succeeded", ["prepare", "verify"]))
        self.assertEqual({path.name: path.read_bytes() for path in self.mesh.parent.iterdir()}, before)
        self.assertEqual(list((self.root / "exports/model").glob(".source-*")), [])

    def test_failed_blender_step_keeps_evidence_and_returns_nothing(self):
        for mode, message in (("prepare-fails", "prepare failed"), ("verify-fails", "reopening")):
            with self.subTest(mode=mode):
                shutil.rmtree(self.root / "exports", ignore_errors=True)
                self.mode.write_text(mode)
                with self.assertRaisesRegex((RuntimeError, ValueError), message):
                    self.export()
                state = json.loads((self.root / "exports/model/export.json").read_text())
                self.assertEqual(state["state"], "failed")
                self.assertTrue(self.mesh.is_file())

    def test_refuses_unsafe_sources_and_existing_destinations_before_running_blender(self):
        with self.assertRaisesRegex(ValueError, "textured OBJ"):
            export_blender_model(self.root / "dense/missing.obj", self.root / "out", blender=str(self.blender))
        with self.assertRaisesRegex(ValueError, "separate"):
            export_blender_model(self.mesh, self.mesh.parent, blender=str(self.blender))
        with self.assertRaisesRegex(ValueError, "Blender is required"):
            export_blender_model(self.mesh, self.root / "out", blender="no-such-blender-binary")
        (self.root / "exports/model").mkdir(parents=True)
        with self.assertRaises(FileExistsError):
            self.export()
        self.assertEqual(list((self.root / "exports/model").iterdir()), [])

    @unittest.skipUnless(os.environ.get("SCANNER_TEST_NATIVE_MODEL") == "1" and shutil.which("blender"),
                         "set SCANNER_TEST_NATIVE_MODEL=1 with Blender installed")
    def test_native_blender_writes_a_reopenable_model_and_a_square_preview(self):
        Image.new("RGB", (64, 64), (200, 80, 40)).save(self.mesh.parent / "texture.jpg")
        outputs = export_blender_model(self.mesh, self.root / "exports/native", view={
            "image": "a.jpg", "location": [0.3, 0.3, 4.0], "up": [0.0, 1.0, 0.0]})
        report = json.loads(Path(outputs["model_verification"]).read_text())
        self.assertEqual((report["status"], report["assets"]["blend"]["faces"], report["assets"]["glb"]["faces"]),
                         ("passed", 1, 1))
        self.assertEqual(report["assets"]["preview"]["view"], "capture_photo")
        with Image.open(outputs["model_preview"]) as preview:
            self.assertEqual((preview.size, preview.mode), ((768, 768), "RGBA"))
            self.assertGreater(preview.getchannel("A").getextrema()[1], 0)
        self.assertEqual(Path(outputs["model_glb"]).read_bytes()[:4], b"glTF")


class ModelLibraryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.files = {}
        for name in ("model_blend", "model_glb", "model_preview", "textured_bundle", "retro_glb"):
            path = self.root / "job" / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(name)
            self.files[name] = str(path)

    def test_folder_is_named_by_capture_time_without_the_upload_id(self):
        self.assertEqual(library_name("scan_2026_10_02_20_53_18_1d6bf263-8bdd-4f13"), "scan_2026_10_02_20_53_18")
        self.assertEqual(library_name("My Bear (final)/../x"), "My-Bear-final-..-x")
        self.assertEqual(library_name("///"), "scan")

    def test_copies_everyday_files_and_never_reuses_a_folder(self):
        outputs = {**self.files, "processing_report": str(self.root / "job/report.json"),
                   "retro_blend": str(self.root / "job/absent.blend")}
        first = publish_to_library(self.root / "library", "scan_2026_10_02_20_53_18_abc", outputs)
        self.assertEqual(sorted(path.name for path in first.iterdir()), [
            "preview.png", "scan_2026_10_02_20_53_18-obj.zip", "scan_2026_10_02_20_53_18-retro.glb",
            "scan_2026_10_02_20_53_18.blend", "scan_2026_10_02_20_53_18.glb"])
        self.assertEqual((first / "scan_2026_10_02_20_53_18.blend").read_text(), "model_blend")
        second = publish_to_library(self.root / "library", "scan_2026_10_02_20_53_18_abc", outputs)
        self.assertEqual((first.name, second.name), ("scan_2026_10_02_20_53_18", "scan_2026_10_02_20_53_18-2"))
        self.assertTrue(Path(self.files["model_blend"]).is_file())

    def test_nothing_to_copy_or_a_failed_copy_leaves_no_folder_behind(self):
        with self.assertRaisesRegex(ValueError, "no finished model"):
            publish_to_library(self.root / "library", "scan", {"processing_report": "x"})
        self.assertFalse((self.root / "library").exists())
        with patch("app.model_library.shutil.copyfile", side_effect=OSError("disk full")), self.assertRaises(OSError):
            publish_to_library(self.root / "library", "scan", self.files)
        self.assertEqual(list((self.root / "library").iterdir()), [])


class ObjectPipelineDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_heavy_work())
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        single_box_scan(self.root)

    def run_pipeline(self, delivery):
        frames = load_scan_metadata(self.root / "metadata").frames
        with patch.object(Sam2Runtime, "validate"), patch("app.sam2_generator.subprocess.run", side_effect=fake_worker):
            generation = generate_mask_proposals(self.root, frames)

        def native_command(command, **kwargs):
            if command[0] == "DensifyPointCloud":
                (self.root / "dense/scene_dense.ply").write_text(
                    "ply\nformat ascii 1.0\nelement vertex 3\nend_header\n")
            return SimpleNamespace(returncode=0)

        patches = {
            "generate_mask_proposals": {"return_value": generation},
            "prepare_colmap_sparse_execution": {"return_value": ([["colmap", "matching"], ["colmap", "mapping"]], [])},
            "record_colmap_intake": {"return_value": {"registered_image_count": len(frames)}},
            "convert_capture_mask_set": {"return_value": SimpleNamespace(as_dict=lambda: {})},
            "validate_openmvs_config_masks": {}, "stage_openmvs_texture_masks": {},
            "write_texture_report": {"return_value": {"warnings": []}},
            "export_camera_view": {"return_value": {"image": "frame_000000.jpg", "location": [0, 0, 1], "up": [0, 1, 0]}},
            "export_blender_model": delivery,
        }
        mocks = {name: self.enterContext(patch(f"app.object_pipeline.{name}", **options))
                 for name, options in patches.items()}
        messages = []
        with patch("app.object_pipeline.subprocess.run", side_effect=native_command):
            outputs = reconstruct_object(validate_and_report_scan(self.root), "preview", progress=messages.append)
        steps = json.loads((self.root / "metadata/processing.json").read_text())["steps"]
        return outputs, steps, messages, mocks

    def test_blender_files_join_the_outputs_and_use_the_boxed_photo_for_the_preview(self):
        delivered = {"model_blend": "b", "model_glb": "g", "model_preview": "p"}
        outputs, steps, messages, mocks = self.run_pipeline({"return_value": delivered})
        self.assertEqual({name: outputs[name] for name in delivered}, delivered)
        self.assertEqual(mocks["export_camera_view"].call_args.args[3], "frame_000000.jpg")
        self.assertEqual(mocks["export_blender_model"].call_args.kwargs["view"]["location"], [0, 0, 1])
        self.assertEqual((steps["model_delivery"]["state"], steps["model_delivery"]["preview_view"]),
                         ("succeeded", "capture_photo"))
        self.assertEqual(steps["automatic_object"]["state"], "succeeded")
        self.assertEqual(steps["automatic_object"]["commands"][-1]["phase"], "TextureMesh")
        self.assertEqual(messages[0], step_message(1))
        self.assertEqual(messages[-1], f"Step {len(OBJECT_STEPS)} of {len(OBJECT_STEPS)}: Making Blender files.")
        numbers = [int(message.split()[1]) for message in messages]
        self.assertEqual(numbers, sorted(numbers))

    def test_blender_problem_is_recorded_but_keeps_the_textured_result(self):
        outputs, steps, _, _ = self.run_pipeline({"side_effect": RuntimeError("Blender verify failed")})
        self.assertIn("textured_mesh", outputs)
        self.assertNotIn("model_blend", outputs)
        self.assertEqual((steps["model_delivery"]["state"], steps["model_delivery"]["error"]),
                         ("failed", "Blender verify failed"))
        self.assertEqual(steps["automatic_object"]["state"], "succeeded")


@unittest.skipUnless(importlib.util.find_spec("fastapi"), "FastAPI runtime not installed")
class ObjectCompletionTests(unittest.TestCase):
    def setUp(self):
        from app import main
        self.main = main
        self.enterContext(isolated_heavy_work())
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for setting in ("INCOMING_DIR", "PROCESSING_DIR", "COMPLETED_DIR", "FAILED_DIR"):
            directory = self.root / setting.lower()
            directory.mkdir()
            self.enterContext(patch.object(main, setting, directory))
        self.jobs = JobStore(self.root / "jobs")
        self.enterContext(patch.object(main, "jobs", self.jobs))
        source = self.root / "source/scan_2026_11_18_09_30_00_abcdef"
        source.mkdir(parents=True)
        single_box_scan(source)
        self.archive = main.INCOMING_DIR / "job.zip"
        with zipfile.ZipFile(self.archive, "w") as output:
            for path in sorted(source.rglob("*")):
                if path.is_file():
                    output.write(path, path.relative_to(source.parent))
        self.jobs.create("job")

    def complete(self, *, blender_files=True):
        def reconstructed(package, preset, **kwargs):
            outputs = {"textured_mesh": str(textured_obj(package.scan_root / "dense"))}
            if blender_files:
                model = package.scan_root / "exports/model"
                model.mkdir(parents=True)
                for key, name in (("model_blend", "model.blend"), ("model_glb", "model.glb"),
                                  ("model_preview", "preview.png")):
                    (model / name).write_text(key)
                    outputs[key] = str(model / name)
            return outputs
        with patch.object(self.main, "reconstruct_object", side_effect=reconstructed):
            self.main.process_scan("job", self.archive, True, True, object_preset="preview")
        return self.jobs.read("job")

    def test_finished_model_is_copied_to_the_library_and_the_message_says_where(self):
        library = self.root / "ScannerModels"
        with patch.object(self.main, "LIBRARY_DIR", library):
            job = self.complete()
        folder = library / "scan_2026_11_18_09_30_00"
        self.assertEqual((job.status, job.message), ("complete", f"Model ready. Saved to {folder}."))
        self.assertEqual(job.outputs["library_dir"], str(folder))
        self.assertEqual(sorted(path.name for path in folder.iterdir()), [
            "preview.png", "scan_2026_11_18_09_30_00-obj.zip",
            "scan_2026_11_18_09_30_00.blend", "scan_2026_11_18_09_30_00.glb"])
        names = {artifact.name for artifact in self.main.list_scan_artifacts("job")}
        self.assertTrue({"model_blend", "model_glb", "model_preview", "textured_bundle"} <= names)
        self.assertNotIn("library_dir", names)

    def test_without_a_library_the_model_stays_downloadable_only(self):
        job = self.complete()
        self.assertEqual(job.message, "Model ready.")
        self.assertNotIn("library_dir", job.outputs)

    def test_missing_blender_files_and_library_problems_are_stated_not_hidden(self):
        blocked = self.root / "not-a-directory"
        blocked.write_text("file in the way")
        with patch.object(self.main, "LIBRARY_DIR", blocked / "library"):
            job = self.complete(blender_files=False)
        self.assertEqual(job.status, "complete")
        self.assertIn("Blender files could not be made", job.message)
        self.assertIn("could not be copied to the model library", job.message)
        self.assertIn("textured_bundle", job.outputs)

    def test_restart_recovery_rediscovers_blender_files(self):
        job = self.complete()
        scan = Path(job.outputs["package_dir"]) / "scan_2026_11_18_09_30_00_abcdef"
        found = discover_standard_output_paths(scan)
        self.assertEqual(Path(found["model_blend"]).name, "model.blend")
        self.assertEqual(Path(found["model_preview"]).name, "preview.png")


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_finished_library_model_offers_blender_and_folder(self):
        folder = self.root / "scan_2026_11_18_09_30_00"
        folder.mkdir()
        (folder / "scan_2026_11_18_09_30_00-retro.blend").write_text("retro")
        (folder / "scan_2026_11_18_09_30_00.blend").write_text("model")
        details = notifier.notification({"scan_id": "job", "status": "complete", "message": "Model ready.",
                                         "outputs": {"library_dir": str(folder)}})
        self.assertEqual((details["title"], details["body"]), ("Your 3D model is ready", folder.name))
        self.assertEqual(details["actions"]["blender"], ("Open in Blender", folder / "scan_2026_11_18_09_30_00.blend"))
        self.assertEqual(details["actions"]["folder"], ("Show folder", folder))

    def test_model_without_a_library_opens_from_the_job_folder(self):
        blend = self.root / "exports/model/model.blend"
        blend.parent.mkdir(parents=True)
        blend.write_text("model")
        details = notifier.notification({"scan_id": "job", "status": "complete", "message": "Model ready.",
                                         "outputs": {"model_blend": str(blend)}})
        self.assertEqual((details["body"], details["actions"]["folder"][1]), ("Model ready.", blend.parent))

    def test_results_without_blender_files_and_failures_have_no_actions(self):
        plain = notifier.notification({"scan_id": "job", "status": "complete", "message": "Reconstruction completed.",
                                       "outputs": {"model_blend": str(self.root / "gone.blend")}})
        self.assertEqual((plain["title"], plain["actions"]), ("Scanner job finished", {}))
        failed = notifier.notification({"scan_id": "job", "status": "failed", "message": "Too few photos aligned"})
        self.assertEqual((failed["urgency"], failed["body"], failed["actions"]),
                         ("critical", "Too few photos aligned", {}))

    def test_chosen_action_opens_the_matching_path(self):
        blend = self.root / "model.blend"
        blend.write_text("model")
        details = notifier.notification({"scan_id": "job", "status": "complete", "message": "Model ready.",
                                         "outputs": {"model_blend": str(blend)}})
        opened, threads = [], []
        chosen = SimpleNamespace(stdout="folder\n")

        class Inline:
            def __init__(self, target, daemon):
                threads.append(target)
            def start(self):
                threads[-1]()

        with patch.object(notifier.subprocess, "run", return_value=chosen) as run, \
                patch.object(notifier.threading, "Thread", Inline), \
                patch.object(notifier, "open_path", side_effect=lambda action, path: opened.append((action, path))):
            notifier.show(details)
        command = run.call_args.args[0]
        self.assertIn("--action=blender=Open in Blender", command)
        self.assertEqual(opened, [("folder", self.root)])


if __name__ == "__main__":
    unittest.main()
