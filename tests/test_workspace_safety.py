from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.scan_metadata import ScanMetadataError, load_scan_metadata
from app.scan_package import prepare_scan_source, scan_id_from_path


def write_scan(root: Path, *, scan_id: str = "scan_test") -> Path:
    scan = root / "scan_test"
    (scan / "images").mkdir(parents=True)
    (scan / "metadata").mkdir()
    Image.new("RGB", (32, 32), (120, 100, 80)).save(scan / "images" / "frame.jpg")
    (scan / "metadata" / "frames.json").write_text(json.dumps([
        {"id": 1, "image": "images/frame.jpg", "timestamp": 0, "resolution": [32, 32]},
    ]))
    (scan / "metadata" / "session.json").write_text(json.dumps({"scan_id": scan_id}))
    return scan


def gpu_dry_run(scan: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "reconstruct_gpu.py"), str(scan),
         "--output-root", str(output), "--dry-run", "--skip-dense", "--skip-openmvs"],
        capture_output=True, text=True, check=False,
    )


def setUpModule():
    from heavy_work_fixture import isolated_heavy_work
    unittest.enterModuleContext(isolated_heavy_work())


class WorkspaceSafetyTests(unittest.TestCase):
    def test_metadata_rejects_unsafe_scan_identifiers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scan = write_scan(Path(temporary))
            for scan_id in ("../victim", "/tmp/victim", ".", "..", "a/b", "a\\b", "x" * 256):
                with self.subTest(scan_id=scan_id):
                    (scan / "metadata" / "session.json").write_text(json.dumps({"scan_id": scan_id}))
                    with self.assertRaisesRegex(ScanMetadataError, "scan_id"):
                        load_scan_metadata(scan / "metadata")

    def test_safe_scan_identifiers_and_path_names_remain_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scan = write_scan(Path(temporary), scan_id="scan-001.v2_test")
            self.assertEqual(load_scan_metadata(scan / "metadata").session.scan_id, "scan-001.v2_test")
        self.assertEqual(scan_id_from_path(Path("scan 001.zip")), "scan_001")

    def test_preparation_refuses_an_existing_destination_without_removing_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scan = write_scan(root / "input")
            destination = root / "prepared"
            prepared = prepare_scan_source(scan, destination)
            result = prepared / "prior-result.ply"
            result.write_text("preserve previous result")
            with self.assertRaises(FileExistsError):
                prepare_scan_source(scan, destination)
            self.assertEqual(result.read_text(), "preserve previous result")

    def test_preparation_refuses_input_as_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scan = write_scan(Path(temporary))
            with self.assertRaises(ValueError):
                prepare_scan_source(scan, scan)
            self.assertTrue((scan / "images" / "frame.jpg").is_file())

    def test_preparation_rejects_overlap_in_both_directions_and_through_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scan = write_scan(root / "input")
            alias = root / "input-alias"
            alias.symlink_to(scan, target_is_directory=True)
            for destination in (scan / "output", scan.parent, alias / "output"):
                with self.subTest(destination=destination):
                    with self.assertRaisesRegex(ValueError, "must not overlap"):
                        prepare_scan_source(scan, destination)
            self.assertTrue((scan / "images" / "frame.jpg").is_file())
            self.assertFalse((scan / "output").exists())

    def test_preparation_refuses_an_existing_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scan = write_scan(root / "input")
            destination = root / "prepared"
            destination.mkdir()
            with self.assertRaises(FileExistsError):
                prepare_scan_source(scan, destination)
            self.assertEqual(list(destination.iterdir()), [])

    def test_preparation_rejects_archive_stored_inside_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "scan.zip"
            with zipfile.ZipFile(archive, "w"):
                pass
            original = archive.read_bytes()
            with self.assertRaisesRegex(ValueError, "must not overlap"):
                prepare_scan_source(archive, root)
            self.assertEqual(archive.read_bytes(), original)

    def test_gpu_rejects_traversal_without_touching_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scan = write_scan(root / "input", scan_id="../victim")
            victim = root / "victim"
            victim.mkdir()
            sentinel = victim / "keep.txt"
            sentinel.write_text("original")
            result = gpu_dry_run(scan, root / "output")
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(sentinel.is_file())
            self.assertEqual(sentinel.read_text(), "original")

    def test_gpu_refuses_existing_filename_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scan = write_scan(root / "input")
            prior = root / "output" / "scan_test" / "source"
            prior.mkdir(parents=True)
            sentinel = prior / "keep.txt"
            sentinel.write_text("original")
            result = gpu_dry_run(scan, root / "output")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(sentinel.read_text(), "original")

    def test_gpu_refuses_existing_metadata_named_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scan = write_scan(root / "input", scan_id="canonical_scan")
            prior = root / "output" / "canonical_scan"
            prior.mkdir(parents=True)
            sentinel = prior / "keep.txt"
            sentinel.write_text("original")
            result = gpu_dry_run(scan, root / "output")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(sentinel.read_text(), "original")

    def test_gpu_still_uses_safe_metadata_named_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scan = write_scan(root / "input", scan_id="canonical_scan")
            result = gpu_dry_run(scan, root / "output")
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((root / "output" / "canonical_scan" / "report.json").read_text())
            self.assertEqual(report["scan_id"], "canonical_scan")
            self.assertTrue(Path(report["scan_root"]).is_dir())

    def test_gpu_rejects_absolute_metadata_scan_id_and_workspace_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            victim = root / "victim"
            victim.mkdir()
            sentinel = victim / "keep.txt"
            sentinel.write_text("original")
            scan = write_scan(root / "input", scan_id=str(victim))
            result = gpu_dry_run(scan, root / "output")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(sentinel.read_text(), "original")

            alternate_output = root / "alternate_output"
            alternate_output.mkdir()
            (alternate_output / "scan_test").symlink_to(victim, target_is_directory=True)
            result = gpu_dry_run(scan, alternate_output)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(sentinel.read_text(), "original")

    def test_gpu_rejects_output_inside_input_before_creating_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scan = write_scan(Path(temporary))
            result = gpu_dry_run(scan, scan / "output")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must not overlap", result.stderr)
            self.assertFalse((scan / "output").exists())

    def test_local_reconstruction_requires_persistent_storage_before_running(self) -> None:
        spec = importlib.util.spec_from_file_location("reconstruct_local_safety", ROOT / "scripts" / "reconstruct_local.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            scan = write_scan(Path(temporary))
            with patch.object(sys, "argv", ["reconstruct_local.py", str(scan), "--run-colmap"]), \
                 patch.object(module, "run_colmap_pipeline", return_value=Path("output.ply")) as run, \
                 self.assertRaises(SystemExit) as error:
                module.main()
            self.assertEqual(error.exception.code, 2)
            run.assert_not_called()

    def test_local_reconstruction_retains_outputs_in_explicit_workspace(self) -> None:
        spec = importlib.util.spec_from_file_location("reconstruct_local_persistent", ROOT / "scripts" / "reconstruct_local.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scan = write_scan(root / "input")
            workspace = root / "retained"

            def reconstruct(scan_root, config, *, include_dense):
                output = scan_root / "result.ply"
                output.write_text("reconstructed output")
                return output

            with patch.object(sys, "argv", ["reconstruct_local.py", str(scan), "--run-colmap", "--work-dir", str(workspace)]), \
                 patch.object(module, "run_colmap_pipeline", side_effect=reconstruct):
                module.main()
            self.assertEqual((workspace / "scan_test" / "result.ply").read_text(), "reconstructed output")
            self.assertTrue((workspace / "scan_test" / "metadata" / "processing.json").is_file())

    def test_preparation_of_fresh_archive_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scan = write_scan(root / "input")
            archive = root / "scan.zip"
            with zipfile.ZipFile(archive, "w") as output:
                for path in scan.rglob("*"):
                    if path.is_file():
                        output.write(path, path.relative_to(scan.parent))
            original = archive.read_bytes()
            prepared = prepare_scan_source(archive, root / "prepared")
            self.assertTrue((prepared / "images" / "frame.jpg").is_file())
            self.assertEqual(archive.read_bytes(), original)

    def test_directory_preparation_does_not_follow_links_in_input_or_output_folders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scan = write_scan(root / "input")
            unrelated = root / "unrelated.jpg"
            unrelated.write_bytes(b"not scan input")
            link = scan / "images" / "linked.jpg"
            link.symlink_to(unrelated)
            with self.assertRaisesRegex(ValueError, "symbolic links"):
                prepare_scan_source(scan, root / "prepared")
            self.assertEqual(unrelated.read_bytes(), b"not scan input")

            link.unlink()
            (scan / "dense").symlink_to(root / "outside-output", target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symbolic links"):
                prepare_scan_source(scan, root / "prepared-output-link")
            self.assertFalse((root / "outside-output").exists())


if __name__ == "__main__":
    unittest.main()
