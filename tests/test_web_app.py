from __future__ import annotations

from io import BytesIO
import importlib.util
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.jobs import JobStore
from heavy_work_fixture import isolated_heavy_work
from test_object_box import phone_scan


@unittest.skipUnless(importlib.util.find_spec("fastapi"), "FastAPI runtime not installed")
class WebAppTests(unittest.TestCase):
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

    def paused(self, scan_id="job"):
        source = self.root / f"source-{scan_id}/capture"
        source.mkdir(parents=True)
        phone_scan(source)
        archive = self.main.INCOMING_DIR / f"{scan_id}.zip"
        with zipfile.ZipFile(archive, "w") as output:
            for path in sorted(source.rglob("*")):
                if path.is_file():
                    output.write(path, path.relative_to(source.parent))
        self.jobs.create(scan_id)
        self.main.process_scan(scan_id, archive, True, True, object_preset="preview")
        return archive

    def finish(self, scan_id="job", *, preview=True):
        from fastapi import BackgroundTasks
        background = BackgroundTasks()
        self.main.start_object_from_box(scan_id, background, {"frame_id": 1, "box": [0.2, 0.2, 0.8, 0.8]})

        def reconstructed(package, preset, **kwargs):
            if preview:
                model = package.scan_root / "exports/model"
                model.mkdir(parents=True)
                Image.new("RGBA", (300, 300), (200, 80, 40, 255)).save(model / "preview.png")
            return {}

        with patch.object(self.main, "reconstruct_object", side_effect=reconstructed):
            background.tasks[0].func(*background.tasks[0].args)
        return self.jobs.read(scan_id)

    def test_page_is_served_and_calls_only_routes_the_backend_has(self):
        response = self.main.web_app()
        page = Path(response.path).read_text()
        self.assertEqual(response.media_type, "text/html")
        self.assertIn("<title>Scanner</title>", page)
        routes = {(method, route.path) for route in self.main.app.routes
                  for method in getattr(route, "methods", ())}
        called = {
            ("GET", "/scans"), ("POST", "/scans"), ("GET", "/scans/{scan_id}"),
            ("DELETE", "/scans/{scan_id}"), ("GET", "/scans/{scan_id}/photos"),
            ("GET", "/scans/{scan_id}/photos/{frame_id}"), ("POST", "/scans/{scan_id}/object-box"),
            ("GET", "/scans/{scan_id}/artifacts"), ("GET", "/scans/{scan_id}/thumbnail"),
            ("GET", "/scans/{scan_id}/files/{relative_path:path}"),
        }
        self.assertTrue(called <= routes, called - routes)
        for fragment in ("/photos", "/object-box", "/artifacts", "/thumbnail", "/files/",
                         "run_reconstruction=true&object_preset=preview"):
            self.assertIn(fragment, page)
        # No third-party script, style or font: the page must work with no internet.
        self.assertEqual(re.findall(r"""(?:src|href)=["']https?://""", page), [])

    def test_thumbnail_is_the_first_photo_until_the_model_preview_exists(self):
        self.paused()
        response = self.main.get_scan_thumbnail("job", max_size=64)
        self.assertEqual(response.media_type, "image/jpeg")
        with Image.open(BytesIO(response.body)) as image:
            self.assertEqual(image.size, (40, 60))
        self.finish()
        response = self.main.get_scan_thumbnail("job", max_size=128)
        self.assertEqual(response.media_type, "image/png")
        with Image.open(BytesIO(response.body)) as image:
            self.assertEqual((image.size, image.mode), ((128, 128), "RGBA"))

    def test_delete_removes_upload_workspace_and_record_but_keeps_the_library_copy(self):
        from fastapi import HTTPException
        archive = self.paused()
        library = self.root / "library/scan"
        library.mkdir(parents=True)
        (library / "scan.blend").write_text("kept")
        job = self.finish()
        self.assertEqual(job.status, "complete")
        self.assertEqual(self.main.delete_scan("job"), {"deleted": "job"})
        self.assertFalse(archive.exists())
        self.assertFalse((self.main.COMPLETED_DIR / "job").exists())
        self.assertEqual(self.jobs.list(), [])
        self.assertEqual((library / "scan.blend").read_text(), "kept")
        with self.assertRaises(HTTPException) as caught:
            self.main.delete_scan("job")
        self.assertEqual(caught.exception.status_code, 404)

    def test_delete_of_a_paused_scan_blocks_a_box_sent_at_the_same_moment(self):
        from fastapi import BackgroundTasks, HTTPException
        self.paused()
        real_rmtree = self.main.shutil.rmtree
        attempts = []

        def racing(path, *args, **kwargs):
            # A box arriving while files are being removed must be refused.
            with self.assertRaises(HTTPException) as caught:
                self.main.start_object_from_box("job", BackgroundTasks(), {"frame_id": 1, "box": [0.2, 0.2, 0.8, 0.8]})
            attempts.append(caught.exception.status_code)
            return real_rmtree(path, *args, **kwargs)

        with patch.object(self.main.shutil, "rmtree", side_effect=racing):
            self.main.delete_scan("job")
        self.assertEqual(attempts, [409])
        self.assertFalse((self.main.PROCESSING_DIR / "job").exists())
        self.assertEqual(self.jobs.list(), [])

    def test_running_scan_cannot_be_deleted(self):
        from fastapi import BackgroundTasks, HTTPException
        archive = self.paused()
        self.main.start_object_from_box("job", BackgroundTasks(), {"frame_id": 1, "box": [0.2, 0.2, 0.8, 0.8]})
        with self.assertRaises(HTTPException) as caught:
            self.main.delete_scan("job")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertTrue(archive.is_file())
        self.assertTrue((self.main.PROCESSING_DIR / "job/capture/images").is_dir())

    def test_delete_removes_a_failed_workspace_and_never_follows_a_link(self):
        self.paused()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("not ours")
        self.jobs.update("job", status="failed", message="fixture")
        (self.main.FAILED_DIR / "job").symlink_to(outside, target_is_directory=True)
        self.main.delete_scan("job")
        self.assertEqual((outside / "keep.txt").read_text(), "not ours")
        self.assertFalse((self.main.FAILED_DIR / "job").is_symlink())
        self.assertFalse((self.main.PROCESSING_DIR / "job").exists())


if __name__ == "__main__":
    unittest.main()
