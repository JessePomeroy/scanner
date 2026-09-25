from __future__ import annotations

import asyncio
from contextlib import ExitStack, closing
from io import BytesIO
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import artifacts, colmap_runner
from app.jobs import JobStore
from app.scan_package import validate_and_report_scan


def setUpModule():
    from heavy_work_fixture import isolated_heavy_work
    unittest.enterModuleContext(isolated_heavy_work())


class BackendCameraAcceptanceTests(unittest.TestCase):
    def test_feature_extraction_exit_zero_cannot_hide_dropped_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "images").mkdir()
            for name in ("a.jpg", "b.jpg"):
                Image.new("RGB", (40, 30)).save(root / "images" / name)

            def fake_command(command):
                if command[1] == "feature_extractor":
                    with closing(sqlite3.connect(root / "database.db")) as db, db:
                        db.execute("CREATE TABLE images (name TEXT, camera_id INTEGER)")
                        db.execute("INSERT INTO images VALUES ('a.jpg', 1)")

            with patch.object(colmap_runner, "run_command", side_effect=fake_command) as run:
                with self.assertRaisesRegex(ValueError, "missing.*b.jpg"):
                    colmap_runner.run_colmap_sparse_pipeline(root)
            self.assertEqual(run.call_count, 1)

    @unittest.skipUnless(os.environ.get("SCANNER_TEST_COLMAP"), "Set SCANNER_TEST_COLMAP for native intake verification")
    def test_native_mixed_resolution_masks_preserve_four_inputs_with_two_cameras(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "images").mkdir()
            masks = root / "masks"
            masks.mkdir()
            names = [f"frame_{index:03}.jpg" for index in range(4)]
            for index, name in enumerate(names):
                size = (80, 64) if index % 2 == 0 else (100, 80)
                image = Image.frombytes("RGB", size, bytes((pixel * 31) % 251 for pixel in range(size[0] * size[1] * 3)))
                exif = Image.Exif()
                exif[271], exif[272] = "Apple", "iPhone test"
                image.save(root / "images" / name, exif=exif)
                Image.new("L", size, color=255).save(masks / (name + ".png"))

            def native_extract_only(command):
                if command[1] == "feature_extractor":
                    result = subprocess.run(
                        command + ["--FeatureExtraction.num_threads", "1"],
                        capture_output=True, text=True, timeout=30,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                elif command[1] == "mapper":
                    # This bounded native test covers extraction, not synthetic
                    # camera registration. The mapper contract is exercised below.
                    (root / "sparse" / "0").mkdir()
                    (root / "sparse" / "0" / "images.bin").write_bytes(struct.pack("<Q", 4))

            with patch.object(colmap_runner, "run_command", side_effect=native_extract_only):
                colmap_runner.run_colmap_sparse_pipeline(
                    root, colmap_runner.ColmapConfig(
                        executable=os.environ["SCANNER_TEST_COLMAP"],
                        use_gpu=False, feature_mask_path=masks,
                    ),
                )
            report = json.loads((root / "metadata" / "colmap_intake.json").read_text())
            self.assertEqual(report["imported_image_count"], 4)
            self.assertEqual(report["camera_count"], 2)
            self.assertEqual(sorted(path.name for path in (root / "images").iterdir()), names)
            with closing(sqlite3.connect(root / "database.db")) as db:
                actual = db.execute("SELECT name, camera_id FROM images ORDER BY name").fetchall()
            self.assertEqual([row[0] for row in actual], names)
            self.assertEqual(actual[0][1], actual[2][1])
            self.assertEqual(actual[1][1], actual[3][1])
            self.assertNotEqual(actual[0][1], actual[1][1])


class GPUFlowAcceptanceTests(unittest.TestCase):
    @staticmethod
    def write_scan(root):
        scan = root / "capture"
        (scan / "images").mkdir(parents=True)
        (scan / "metadata").mkdir()
        frames = []
        for index, size in enumerate(((32, 32), (64, 48))):
            name = f"frame_{index:03}.jpg"
            Image.new("RGB", size, color=(100, 110, 120)).save(scan / "images" / name)
            frames.append({"id": index, "image": f"images/{name}", "timestamp": index, "resolution": size})
        (scan / "metadata" / "frames.json").write_text(json.dumps(frames))
        (scan / "metadata" / "session.json").write_text(json.dumps({"scan_id": "capture"}))
        return scan

    def test_dry_run_reports_replayable_resolution_batches_without_renaming_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scan = self.write_scan(root)
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "reconstruct_gpu.py"), str(scan),
                 "--output-root", str(root / "output"), "--dry-run", "--skip-openmvs", "--skip-dense"],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((root / "output" / "capture" / "report.json").read_text())
            features = [command for command in report["commands"] if command[1] == "feature_extractor"]
            self.assertEqual(len(features), 2)
            self.assertEqual(report["effective_camera_sharing"], "per_resolution")
            self.assertEqual(report["camera_groups"], {"32x32": 1, "64x48": 1})
            for command in features:
                image_list = Path(command[command.index("--image_list_path") + 1])
                self.assertTrue(image_list.is_file())
                self.assertTrue(image_list.read_text().startswith("frame_"))
            self.assertIsNone(report["colmap_intake"])
            self.assertFalse((Path(report["scan_root"]) / "database.db").exists())

    def test_mixed_batch_preparation_preserves_package_revalidation(self):
        with tempfile.TemporaryDirectory() as tmp:
            scan = self.write_scan(Path(tmp))
            before = validate_and_report_scan(scan)
            commands, groups = colmap_runner.prepare_colmap_sparse_execution(
                scan, colmap_runner.ColmapConfig()
            )
            after = validate_and_report_scan(scan)
            self.assertEqual(after.validation, before.validation)
            self.assertEqual(len(groups), 2)
            for command in commands[:-2]:
                image_list = Path(command[command.index("--image_list_path") + 1])
                self.assertEqual(image_list.parent, scan / "metadata")
                self.assertTrue(image_list.name.startswith("colmap_images_"))

    def test_exit_zero_texture_command_cannot_mark_missing_materials_complete(self):
        spec = importlib.util.spec_from_file_location("gpu_flow_regression", ROOT / "scripts" / "reconstruct_gpu.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scan = self.write_scan(root)
            output = root / "output"

            def successful_native_command(command, **_kwargs):
                if command[1] == "feature_extractor":
                    database = Path(command[command.index("--database_path") + 1])
                    names = Path(command[command.index("--image_list_path") + 1]).read_text().splitlines()
                    images = Path(command[command.index("--image_path") + 1])
                    with closing(sqlite3.connect(database)) as db, db:
                        db.execute("CREATE TABLE IF NOT EXISTS images (name TEXT, camera_id INTEGER)")
                        db.execute("CREATE TABLE IF NOT EXISTS cameras (camera_id INTEGER, width INTEGER, height INTEGER)")
                        for name in names:
                            with Image.open(images / name) as image:
                                width, height = image.size
                            db.execute("INSERT INTO images VALUES (?, ?)", (name, width))
                            db.execute("INSERT INTO cameras VALUES (?, ?, ?)", (width, width, height))
                elif command[1] == "mapper":
                    sparse = Path(command[command.index("--output_path") + 1]) / "0"
                    sparse.mkdir()
                    (sparse / "images.bin").write_bytes(struct.pack("<Q", 2))
                elif command[0] == "TextureMesh":
                    dense = output / "capture" / "source" / "capture" / "dense"
                    (dense / "scene_textured.obj").write_text(
                        "mtllib absent.mtl\nvt 0 0\nvt 1 0\nvt 0 1\nusemtl missing\nf 1/1 2/2 3/3\n"
                    )

            with (
                patch.object(sys, "argv", ["reconstruct_gpu.py", str(scan), "--output-root", str(output)]),
                patch.object(module, "run_command", side_effect=successful_native_command),
                patch.object(module, "inspect_openmvs_dense_cloud", return_value=SimpleNamespace(as_dict=lambda: {})),
                self.assertRaisesRegex(ValueError, "absent.mtl"),
            ):
                module.main()
            processing = json.loads((output / "capture" / "source" / "capture" / "metadata" / "processing.json").read_text())
            step = processing["steps"]["gpu_reconstruction"]
            self.assertEqual(step["state"], "failed")
            self.assertEqual(step["failure_phase"], "verification")
            self.assertEqual(step["failed_command"][0], "TextureMesh")
            self.assertFalse((output / "capture" / "report.json").exists())


class TexturedBundleTests(unittest.TestCase):
    def test_bundle_carries_only_referenced_files_and_can_be_served(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp)
            root = storage / "scan"
            root.mkdir()
            obj = root / "mesh.obj"
            obj.write_text("mtllib mesh.mtl\nv 0 0 0\nusemtl material\nf 1 1 1\n")
            (root / "mesh.mtl").write_text("newmtl material\nmap_Kd texture.jpg\n")
            (root / "texture.jpg").write_bytes(b"texture payload")
            (root / "private.txt").write_text("not a dependency")
            result = artifacts.bundle_textured_mesh(obj, root / "delivery.zip")
            with zipfile.ZipFile(result) as archive:
                self.assertEqual(set(archive.namelist()), {"mesh.obj", "mesh.mtl", "texture.jpg"})
                self.assertIsNone(archive.testzip())
            outputs = {"package_dir": str(root), "textured_bundle": str(result)}
            listed = artifacts.list_downloadable_artifacts(outputs, allowed_package_roots=[storage])
            self.assertEqual([entry.name for entry in listed], ["textured_bundle"])

    def test_bundle_rejects_escaping_dependency_and_existing_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "scan"
            root.mkdir()
            obj = root / "mesh.obj"
            obj.write_text("mtllib ../private.mtl\n")
            (root.parent / "private.mtl").write_text("private")
            with self.assertRaises(ValueError):
                artifacts.bundle_textured_mesh(obj, root / "delivery.zip")
            obj.write_text("mtllib mesh.mtl\nusemtl material\n")
            (root / "mesh.mtl").write_text("newmtl material\nmap_Kd image.jpg\n")
            (root / "image.jpg").write_bytes(b"image")
            destination = root / "delivery.zip"
            destination.write_bytes(b"original")
            with self.assertRaises(FileExistsError):
                artifacts.bundle_textured_mesh(obj, destination)
            self.assertEqual(destination.read_bytes(), b"original")

    def test_missing_or_symlinked_texture_cannot_publish_a_partial_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            obj = root / "mesh.obj"
            obj.write_text("mtllib mesh.mtl\nusemtl material\n")
            (root / "mesh.mtl").write_text("newmtl material\nmap_Kd image.jpg\n")
            destination = root / "result.zip"
            with self.assertRaises(FileNotFoundError):
                artifacts.bundle_textured_mesh(obj, destination)
            self.assertFalse(destination.exists())
            (root / "secret.jpg").write_bytes(b"private")
            (root / "image.jpg").symlink_to(root / "secret.jpg")
            with self.assertRaises(ValueError):
                artifacts.bundle_textured_mesh(obj, destination)
            self.assertFalse(destination.exists())


@unittest.skipUnless(importlib.util.find_spec("fastapi"), "FastAPI is not installed")
class UploadValidationLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from app import main
        self.main = main
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        for setting in ("INCOMING_DIR", "PROCESSING_DIR", "COMPLETED_DIR", "FAILED_DIR"):
            directory = self.root / setting.lower()
            directory.mkdir()
            self.patches.enter_context(patch.object(main, setting, directory))
        self.store = JobStore(self.root / "jobs")
        self.patches.enter_context(patch.object(main, "jobs", self.store))

    async def upload(self):
        from fastapi import BackgroundTasks, UploadFile
        return await self.main.upload_scan(
            BackgroundTasks(), UploadFile(file=BytesIO(b"diagnostic fixture")),
            run_reconstruction=False, run_dense=False, run_openmvs=False,
            scope_mode="auto_roi", use_masks=False, mask_profile="scene_geometry",
            review_scope=False,
        )

    async def test_validation_runs_off_event_loop_and_failure_is_terminal(self):
        loop_thread = threading.get_ident()
        worker_threads = []

        def extract_failure(*args):
            worker_threads.append(threading.get_ident())
            raise OSError("injected validation I/O failure")

        with patch.object(self.main, "safe_extract_zip", side_effect=extract_failure):
            result = await self.upload()
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.stage, "finished")
        self.assertTrue(worker_threads)
        self.assertNotIn(loop_thread, worker_threads)
        self.assertIn("injected validation I/O failure", result.message)

    async def test_partial_extract_is_preserved_and_move_failure_does_not_hide_primary(self):
        def extract_failure(_archive, destination):
            (destination / "partial.txt").write_text("recoverable")
            raise OSError("original extraction error")

        with patch.object(self.main, "safe_extract_zip", side_effect=extract_failure):
            result = await self.upload()
        self.assertEqual(result.status, "failed")
        self.assertTrue((self.main.FAILED_DIR / result.scan_id / "partial.txt").exists())

        with (
            patch.object(self.main, "safe_extract_zip", side_effect=extract_failure),
            patch.object(self.main, "fail_processing", side_effect=OSError("move failed")),
        ):
            result = await self.upload()
        self.assertEqual(result.status, "failed")
        self.assertIn("original extraction error", result.message)
        self.assertTrue((self.main.PROCESSING_DIR / result.scan_id / "partial.txt").exists())

    async def test_existing_workspace_is_never_claimed_or_removed(self):
        scan_id = "existing"
        self.store.create(scan_id)
        workspace = self.main.PROCESSING_DIR / scan_id
        workspace.mkdir()
        (workspace / "prior.txt").write_text("keep prior work")
        result = self.main.validate_uploaded_scan(scan_id, self.root / "unused.zip")
        self.assertEqual(result.status, "failed")
        self.assertEqual((workspace / "prior.txt").read_text(), "keep prior work")
        self.assertFalse((self.main.FAILED_DIR / scan_id).exists())

    async def test_cancelled_http_wait_still_finishes_validation_lifecycle(self):
        started = asyncio.Event()
        release = threading.Event()
        finished = threading.Event()
        loop = asyncio.get_running_loop()

        def delayed_error(_archive, destination):
            (destination / "partial.txt").write_text("recoverable")
            loop.call_soon_threadsafe(started.set)
            release.wait(timeout=5)
            finished.set()
            raise OSError("validation completed after disconnect")

        with patch.object(self.main, "safe_extract_zip", side_effect=delayed_error):
            task = asyncio.create_task(self.upload())
            await asyncio.wait_for(started.wait(), timeout=5)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.to_thread(finished.wait, 5)
            for _ in range(100):
                records = self.store.list(limit=1)
                if records[0].status == "failed":
                    break
                await asyncio.sleep(0.01)
        result = self.store.list(limit=1)[0]
        self.assertEqual(result.status, "failed")
        self.assertTrue((self.main.FAILED_DIR / result.scan_id / "partial.txt").is_file())

    async def test_delivery_publishes_bundle_and_quality_evidence_without_claiming_blend(self):
        scan_root = self.root / "scan"
        dense = scan_root / "dense"
        dense.mkdir(parents=True)
        obj = dense / "scene_textured.obj"
        obj.write_text("mtllib scene_textured.mtl\nusemtl material\n")
        (dense / "scene_textured.mtl").write_text("newmtl material\nmap_Kd image.jpg\n")
        (dense / "image.jpg").write_bytes(b"packaging fixture")
        quality = dense / "texture_quality.json"
        quality.write_text('{"status":"needs_review"}')
        outputs = {"textured_mesh": str(obj)}
        self.main.publish_delivery_outputs(scan_root, outputs)
        self.assertEqual(outputs["texture_quality"], str(quality))
        self.assertIn("textured_bundle", outputs)
        self.assertNotIn("blend", outputs)
        with zipfile.ZipFile(outputs["textured_bundle"]) as archive:
            self.assertEqual(archive.read("texture_quality.json"), quality.read_bytes())


if __name__ == "__main__":
    unittest.main()
