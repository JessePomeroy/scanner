from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
import zipfile

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.colmap_runner import build_colmap_commands  # noqa: E402
from app.scan_validator import ScanValidationError, validate_scan_package  # noqa: E402
from app.storage import UnsafeArchiveError, safe_extract_zip  # noqa: E402


class BackendTests(unittest.TestCase):
    def test_validate_scan_package_accepts_valid_minimal_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            report = validate_scan_package(scan_dir)

        self.assertEqual(report.image_count, 1)
        self.assertEqual(report.frame_count, 1)
        self.assertEqual(report.scan_id, "scan_test")

    def test_validate_scan_package_rejects_missing_image_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            (scan_dir / "images" / "frame_000001.jpg").unlink()

            with self.assertRaises(ScanValidationError):
                validate_scan_package(scan_dir)

    def test_colmap_command_sequence_contains_expected_stages(self) -> None:
        commands = build_colmap_commands(Path("/tmp/scan"))
        stages = [command[1] for command in commands]

        self.assertEqual(
            stages,
            [
                "feature_extractor",
                "exhaustive_matcher",
                "mapper",
                "image_undistorter",
                "patch_match_stereo",
                "stereo_fusion",
            ],
        )

    def test_safe_extract_zip_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            archive = tmp_path / "bad.zip"
            with zipfile.ZipFile(archive, "w") as zip_file:
                zip_file.writestr("../escape.txt", "bad")

            with self.assertRaises(UnsafeArchiveError):
                safe_extract_zip(archive, tmp_path / "out")

    def _write_scan(self, root: Path) -> Path:
        scan_dir = root / "scan_test"
        images = scan_dir / "images"
        metadata = scan_dir / "metadata"
        images.mkdir(parents=True)
        metadata.mkdir(parents=True)

        (images / "frame_000001.jpg").write_bytes(b"not a real jpeg")
        (metadata / "frames.json").write_text(
            json.dumps(
                [
                    {
                        "id": 1,
                        "image": "images/frame_000001.jpg",
                        "timestamp": 0.0,
                    }
                ]
            )
        )
        (metadata / "session.json").write_text(json.dumps({"scan_id": "scan_test"}))
        return scan_dir


if __name__ == "__main__":
    unittest.main()
