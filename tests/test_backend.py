from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
import zipfile

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.colmap_runner import (  # noqa: E402
    build_colmap_commands,
    build_colmap_dense_commands,
    build_colmap_sparse_commands,
)
from app.report_writer import write_scan_report  # noqa: E402
from app.scan_package import prepare_scan_source, scan_id_from_path, validate_and_report_scan  # noqa: E402
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
        self.assertEqual(report.scan_mode, "scene_scan")

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
                "sequential_matcher",
                "mapper",
                "image_undistorter",
                "patch_match_stereo",
                "stereo_fusion",
            ],
        )

    def test_colmap_gpu_flags_match_colmap_4_option_names(self) -> None:
        commands = build_colmap_commands(Path("/tmp/scan"))

        self.assertIn("--FeatureExtraction.use_gpu", commands[0])
        self.assertIn("--FeatureMatching.use_gpu", commands[1])

    def test_colmap_commands_can_be_split_by_sparse_and_dense_stages(self) -> None:
        sparse_commands = build_colmap_sparse_commands(Path("/tmp/scan"))
        dense_commands = build_colmap_dense_commands(Path("/tmp/scan"))

        self.assertEqual([command[1] for command in sparse_commands], [
            "feature_extractor",
            "sequential_matcher",
            "mapper",
        ])
        self.assertEqual([command[1] for command in dense_commands], [
            "image_undistorter",
            "patch_match_stereo",
            "stereo_fusion",
        ])

    def test_safe_extract_zip_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            archive = tmp_path / "bad.zip"
            with zipfile.ZipFile(archive, "w") as zip_file:
                zip_file.writestr("../escape.txt", "bad")

            with self.assertRaises(UnsafeArchiveError):
                safe_extract_zip(archive, tmp_path / "out")

    def test_validate_scan_package_accepts_object_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            (scan_dir / "metadata" / "session.json").write_text(
                json.dumps(
                    {
                        "scan_id": "scan_test",
                        "scan_mode": "object_scan",
                        "object_center_world": [1.0, 2.0, 3.0],
                        "object_radius_meters": 1.5,
                    }
                )
            )

            report = validate_scan_package(scan_dir)

        self.assertEqual(report.scan_mode, "object_scan")
        self.assertEqual(report.object_center_world, [1.0, 2.0, 3.0])
        self.assertEqual(report.object_radius_meters, 1.5)

    def test_write_scan_report_summarizes_capture_quality(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            report = validate_scan_package(scan_dir)

            report_path = write_scan_report(scan_dir, report)
            payload = json.loads(report_path.read_text())

        self.assertEqual(payload["capture"]["image_count"], 1)
        self.assertEqual(payload["capture"]["blur"]["mean"], 0.42)
        self.assertEqual(payload["capture"]["movement_delta_meters"]["max"], 0.12)
        self.assertIn("low_frame_count", payload["warnings"])

    def test_write_scan_report_marks_object_crop_alignment_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            (scan_dir / "metadata" / "session.json").write_text(
                json.dumps(
                    {
                        "scan_id": "scan_test",
                        "scan_mode": "object_scan",
                        "object_center_world": [1.0, 2.0, 3.0],
                        "object_radius_meters": 1.5,
                    }
                )
            )
            report = validate_scan_package(scan_dir)

            report_path = write_scan_report(scan_dir, report)
            payload = json.loads(report_path.read_text())

        self.assertTrue(payload["object_scan"]["ready_for_manual_radius_crop"])
        self.assertEqual(
            payload["object_scan"]["automatic_crop_status"],
            "needs_arkit_to_colmap_alignment",
        )

    def test_scan_package_interface_prepares_validates_and_reports_zip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            scan_dir = self._write_scan(tmp_path)
            archive = tmp_path / "scan_test.zip"
            with zipfile.ZipFile(archive, "w") as zip_file:
                for path in scan_dir.rglob("*"):
                    if path.is_file():
                        zip_file.write(path, path.relative_to(tmp_path))

            scan_root = prepare_scan_source(archive, tmp_path / "prepared")
            package = validate_and_report_scan(scan_root)

            self.assertEqual(package.scan_id, "scan_test")
            self.assertEqual(package.validation.image_count, 1)
            self.assertEqual(package.report_path.name, "scan_report.json")
            self.assertTrue(package.manifest_path.exists())

    def test_scan_package_records_processing_metadata_in_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            package = validate_and_report_scan(scan_dir)
            package.record_processing_step(
                "colmap",
                {
                    "matcher": "sequential_matcher",
                    "elapsed_seconds": 12.5,
                },
            )
            package = validate_and_report_scan(scan_dir)
            payload = json.loads(package.report_path.read_text())

        self.assertEqual(
            payload["processing"]["steps"]["colmap"]["matcher"],
            "sequential_matcher",
        )
        self.assertEqual(payload["processing"]["steps"]["colmap"]["elapsed_seconds"], 12.5)

    def test_scan_report_does_not_warn_for_normal_keyframe_throttling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            session_path = scan_dir / "metadata" / "session.json"
            session = json.loads(session_path.read_text())
            session.update(
                {
                    "rejected_frame_count": 1000,
                    "rejected_motion_count": 1000,
                    "rejected_tracking_count": 0,
                    "rejected_blur_count": 0,
                }
            )
            session_path.write_text(json.dumps(session))
            report = validate_scan_package(scan_dir)
            report_path = write_scan_report(scan_dir, report)
            payload = json.loads(report_path.read_text())

        self.assertNotIn("high_rejected_frame_count", payload["warnings"])
        self.assertNotIn("tracking_loss_during_capture", payload["warnings"])
        self.assertNotIn("many_blurry_rejected_frames", payload["warnings"])

    def test_scan_id_from_path_handles_zip_and_spaces(self) -> None:
        self.assertEqual(scan_id_from_path(Path("scan 001.zip")), "scan_001")
        self.assertEqual(scan_id_from_path(Path("scan_002")), "scan_002")

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
                        "tracking_state": "normal",
                        "blur_score": 0.42,
                        "movement_delta_meters": 0.12,
                        "rotation_delta_degrees": 8.0,
                        "movement_speed_meters_per_second": 0.25,
                        "resolution": [1920, 1080],
                    }
                ]
            )
        )
        (metadata / "session.json").write_text(
            json.dumps({"scan_id": "scan_test", "scan_mode": "scene_scan"})
        )
        return scan_dir


if __name__ == "__main__":
    unittest.main()
