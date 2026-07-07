from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

BLENDER_SCRIPT = ROOT / "scripts" / "blender" / "prepare_scan_asset.py"

from app.colmap_runner import (  # noqa: E402
    build_colmap_commands,
    build_colmap_dense_commands,
    build_colmap_sparse_commands,
)
from app.neural_backend_planner import (  # noqa: E402
    NeuralBackendConfig,
    build_neural_backend_plan,
    write_neural_backend_report,
)
from app.open3d_cleanup import cleanup_outputs  # noqa: E402
from app.point_cloud_processor import (  # noqa: E402
    PointCloudProcessingConfig,
    build_processing_summary,
    process_point_cloud,
    write_processing_report,
)
from app.reconstruction_backends import BackendPlanConfig, build_backend_plan  # noqa: E402
from app.reconstruction_plan import write_command_plan_report  # noqa: E402
from app.report_writer import write_scan_report  # noqa: E402
from app.scan_package import prepare_scan_source, scan_id_from_path, validate_and_report_scan  # noqa: E402
from app.scan_validator import ScanValidationError, validate_scan_package  # noqa: E402
from app.storage import UnsafeArchiveError, safe_extract_zip  # noqa: E402


def load_blender_script_module():
    spec = importlib.util.spec_from_file_location("prepare_scan_asset", BLENDER_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load prepare_scan_asset.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class BackendTests(unittest.TestCase):
    def test_validate_scan_package_accepts_valid_minimal_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            report = validate_scan_package(scan_dir)

        self.assertEqual(report.image_count, 1)
        self.assertEqual(report.frame_count, 1)
        self.assertEqual(report.video_count, 0)
        self.assertEqual(report.scan_id, "scan_test")
        self.assertEqual(report.scan_mode, "scene_scan")

    def test_validate_scan_package_rejects_missing_image_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            (scan_dir / "images" / "frame_000001.jpg").unlink()

            with self.assertRaises(ScanValidationError):
                validate_scan_package(scan_dir)

    def test_validate_scan_package_accepts_optional_video_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            video_dir = scan_dir / "video"
            video_dir.mkdir()
            (video_dir / "scan.mov").write_bytes(b"fake mov")
            (scan_dir / "metadata" / "video.json").write_text(
                json.dumps(
                    [
                        {
                            "path": "video/scan.mov",
                            "captured_at": "2026-07-07T00:00:00Z",
                            "duration_seconds": 12.5,
                            "frame_rate": 30,
                            "resolution": [1920, 1080],
                            "codec": "h264",
                            "includes_audio": False,
                        }
                    ]
                )
            )

            report = validate_scan_package(scan_dir)

        self.assertEqual(report.video_count, 1)

    def test_validate_scan_package_rejects_missing_video_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            (scan_dir / "metadata" / "video.json").write_text(
                json.dumps([{"path": "video/missing.mov"}])
            )

            with self.assertRaises(ScanValidationError):
                validate_scan_package(scan_dir)

    def test_validate_scan_package_rejects_non_video_metadata_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            video_dir = scan_dir / "video"
            video_dir.mkdir()
            (video_dir / "notes.txt").write_text("not a video")
            (scan_dir / "metadata" / "video.json").write_text(
                json.dumps([{"path": "video/notes.txt"}])
            )

            with self.assertRaises(ScanValidationError):
                validate_scan_package(scan_dir)

    def test_validate_scan_package_rejects_video_reference_outside_video_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            preview_dir = scan_dir / "preview"
            preview_dir.mkdir()
            (preview_dir / "scan.mp4").write_bytes(b"fake mp4")
            (scan_dir / "metadata" / "video.json").write_text(
                json.dumps([{"path": "preview/scan.mp4"}])
            )

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

    def test_blender_asset_parser_accepts_supported_options(self) -> None:
        module = load_blender_script_module()
        options = module.parse_blender_args(
            [
                "scan.obj",
                "scan.blend",
                "--scale",
                "0.5",
                "--decimate-ratio",
                "0.25",
                "--texture-dir",
                "textures",
                "--export-glb",
                "scan.glb",
                "--origin",
                "none",
            ]
        )

        self.assertEqual(options.input_path, Path("scan.obj"))
        self.assertEqual(options.output_path, Path("scan.blend"))
        self.assertEqual(options.scale, 0.5)
        self.assertEqual(options.decimate_ratio, 0.25)
        self.assertEqual(options.texture_dir, Path("textures"))
        self.assertEqual(options.export_glb, Path("scan.glb"))
        self.assertEqual(options.origin, "none")

    def test_blender_asset_parser_rejects_bad_decimate_ratio(self) -> None:
        module = load_blender_script_module()

        with self.assertRaises(SystemExit):
            module.parse_blender_args(["scan.obj", "scan.blend", "--decimate-ratio", "2"])

    def test_blender_script_args_use_separator_payload(self) -> None:
        module = load_blender_script_module()

        args = module.blender_script_args(["blender", "--background", "--", "input.ply", "out.blend"])

        self.assertEqual(args, ["input.ply", "out.blend"])

    def test_backend_plan_builds_colmap_openmvs_command_plan(self) -> None:
        plan = build_backend_plan(
            Path("/tmp/scan"),
            BackendPlanConfig(
                backend="colmap_openmvs",
                include_dense=False,
                include_openmvs=False,
            ),
        )

        self.assertEqual(plan.backend, "colmap_openmvs")
        self.assertEqual(
            [command[1] for command in plan.commands],
            [
                "feature_extractor",
                "sequential_matcher",
                "mapper",
                "model_converter",
            ],
        )
        self.assertIn("sparse_point_cloud", plan.outputs)

    def test_backend_plan_rejects_openmvs_without_dense_colmap(self) -> None:
        with self.assertRaises(ValueError):
            build_backend_plan(
                Path("/tmp/scan"),
                BackendPlanConfig(
                    backend="colmap_openmvs",
                    include_dense=False,
                    include_openmvs=True,
                ),
            )

    def test_backend_plan_builds_meshroom_batch_command(self) -> None:
        plan = build_backend_plan(Path("/tmp/scan"), BackendPlanConfig(backend="meshroom"))
        command = plan.commands[0]

        self.assertEqual(plan.backend, "meshroom")
        self.assertEqual(command[0], "meshroom_batch")
        self.assertIn("--input", command)
        self.assertIn("--output", command)
        self.assertIn("published_output", plan.outputs)

    def test_backend_plan_builds_experimental_alicevision_chain(self) -> None:
        plan = build_backend_plan(Path("/tmp/scan"), BackendPlanConfig(backend="alicevision"))

        self.assertEqual(plan.backend, "alicevision")
        self.assertEqual(plan.commands[0][0], "aliceVision_cameraInit")
        self.assertEqual(plan.commands[-1][0], "aliceVision_texturing")
        self.assertGreaterEqual(plan.command_count, 10)
        self.assertIn("textured_output", plan.outputs)

    def test_command_plan_report_writes_json_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "plan.json"
            plan = build_backend_plan(
                Path("/tmp/scan"),
                BackendPlanConfig(
                    backend="colmap_openmvs",
                    include_dense=False,
                    include_openmvs=False,
                ),
            )

            write_command_plan_report(plan, report_path, extra={"scan_id": "scan_test"})
            payload = json.loads(report_path.read_text())

        self.assertEqual(payload["scan_id"], "scan_test")
        self.assertEqual(payload["backend"], "colmap_openmvs")
        self.assertEqual(payload["command_count"], 4)
        self.assertIn("command_lines", payload)

    def test_point_cloud_processing_summary_does_not_import_optional_backends(self) -> None:
        summary = build_processing_summary(
            Path("/tmp/input.ply"),
            Path("/tmp/output.ply"),
            PointCloudProcessingConfig(
                processor="threecrate",
                voxel_size=0.05,
                estimate_normals=True,
                statistical_outlier_neighbors=None,
                statistical_outlier_std_ratio=None,
            ),
        )

        self.assertEqual(summary["processor"], "threecrate")
        self.assertEqual(summary["voxel_size"], 0.05)
        self.assertIn("Experimental optional processing path.", summary["notes"])

    def test_point_cloud_processing_rejects_unknown_processor(self) -> None:
        with self.assertRaises(ValueError):
            build_processing_summary(
                Path("/tmp/input.ply"),
                Path("/tmp/output.ply"),
                PointCloudProcessingConfig(processor="unknown"),
            )

    def test_point_cloud_processing_report_writes_dry_run_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "point_cloud.json"
            write_processing_report(
                Path("/tmp/input.ply"),
                Path("/tmp/output.ply"),
                report_path,
                PointCloudProcessingConfig(processor="open3d", voxel_size=0.1),
                dry_run=True,
            )
            payload = json.loads(report_path.read_text())

        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["processor"], "open3d")
        self.assertEqual(payload["voxel_size"], 0.1)

    def test_threecrate_processor_path_writes_plain_point_cloud_after_normals(self) -> None:
        calls: list[tuple[str, object]] = []

        class FakeCloud:
            def __init__(self, name: str) -> None:
                self.name = name

        class FakeNormalCloud:
            def positions(self) -> str:
                calls.append(("positions", "normal_cloud"))
                return "normal_positions"

        class FakePointCloud:
            @staticmethod
            def from_numpy(value: str) -> FakeCloud:
                calls.append(("from_numpy", value))
                return FakeCloud("plain_from_normals")

        class FakeThreeCrate:
            PointCloud = FakePointCloud

            @staticmethod
            def read_point_cloud(path: str) -> FakeCloud:
                calls.append(("read", path))
                return FakeCloud("read")

            @staticmethod
            def voxel_downsample(cloud: FakeCloud, voxel_size: float) -> FakeCloud:
                calls.append(("voxel", voxel_size))
                return FakeCloud("voxel")

            @staticmethod
            def remove_statistical_outliers(cloud: FakeCloud, neighbors: int, ratio: float) -> FakeCloud:
                calls.append(("outliers", (neighbors, ratio)))
                return FakeCloud("filtered")

            @staticmethod
            def estimate_normals(cloud: FakeCloud) -> FakeNormalCloud:
                calls.append(("normals", cloud.name))
                return FakeNormalCloud()

            @staticmethod
            def write_point_cloud(cloud: FakeCloud, path: str) -> None:
                calls.append(("write", (cloud.name, path)))

        previous = sys.modules.get("threecrate")
        sys.modules["threecrate"] = FakeThreeCrate
        try:
            with tempfile.TemporaryDirectory() as tmp:
                input_path = Path(tmp) / "input.ply"
                output_path = Path(tmp) / "output.ply"
                input_path.write_text("ply")

                result = process_point_cloud(
                    input_path,
                    output_path,
                    PointCloudProcessingConfig(
                        processor="threecrate",
                        voxel_size=0.05,
                        estimate_normals=True,
                        statistical_outlier_neighbors=10,
                        statistical_outlier_std_ratio=1.5,
                    ),
                )
        finally:
            if previous is None:
                sys.modules.pop("threecrate", None)
            else:
                sys.modules["threecrate"] = previous

        self.assertEqual(result, output_path)
        self.assertIn(("voxel", 0.05), calls)
        self.assertIn(("outliers", (10, 1.5)), calls)
        self.assertIn(("from_numpy", "normal_positions"), calls)
        self.assertIn(("write", ("plain_from_normals", str(output_path))), calls)

    def test_cleanup_outputs_fails_before_import_when_dense_cloud_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                cleanup_outputs(Path(tmp))

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
        self.assertEqual(payload["capture"]["video_count"], 0)
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
            manifest = json.loads(package.manifest_path.read_text())
            self.assertEqual(manifest["schema_version"], "0.3.0")
            self.assertEqual(manifest["file_counts"]["videos"], 0)
            self.assertEqual(manifest["file_counts"]["video_metadata_entries"], 0)
            self.assertFalse(manifest["sensors"]["video"])

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

    def test_neural_backend_plan_prefers_video_for_mast3r_slam(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            video_dir = scan_dir / "video"
            video_dir.mkdir()
            (video_dir / "scan.mp4").write_bytes(b"fake mp4")
            (scan_dir / "metadata" / "video.json").write_text(
                json.dumps([{"path": "video/scan.mp4"}])
            )

            plan = build_neural_backend_plan(
                scan_dir,
                NeuralBackendConfig(backend="mast3r_slam"),
            )

        self.assertEqual(plan.backend, "mast3r_slam")
        self.assertEqual(plan.inputs["video_count"], 1)
        self.assertTrue(any("video/scan.mp4" in part for part in plan.commands[0]))

    def test_neural_backend_plan_uses_images_for_depth_anything(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            plan = build_neural_backend_plan(
                scan_dir,
                NeuralBackendConfig(backend="depth_anything"),
            )

        self.assertEqual(plan.backend, "depth_anything")
        self.assertEqual(plan.inputs["image_count"], 1)
        self.assertIn("--encoder", plan.commands[0])
        self.assertIn("vits", plan.commands[0])

    def test_neural_backend_report_writes_json_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            scan_dir = self._write_scan(tmp_path)
            report_path = tmp_path / "neural_plan.json"
            plan = build_neural_backend_plan(
                scan_dir,
                NeuralBackendConfig(backend="lingbot"),
            )

            write_neural_backend_report(plan, report_path)
            payload = json.loads(report_path.read_text())

        self.assertEqual(payload["backend"], "lingbot")
        self.assertEqual(payload["inputs"]["video_count"], 0)
        self.assertEqual(payload["commands"], [])

    def test_neural_backend_cli_resets_stale_zip_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_root = tmp_path / "source"
            archive = tmp_path / "scan_test.zip"
            work_dir = tmp_path / "work dir"

            scan_dir = self._write_scan(source_root)
            video_dir = scan_dir / "video"
            video_dir.mkdir()
            (video_dir / "scan.mp4").write_bytes(b"fake mp4")
            (scan_dir / "metadata" / "video.json").write_text(
                json.dumps([{"path": "video/scan.mp4"}])
            )
            self._zip_scan(scan_dir, archive, source_root)

            first = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "plan_neural_backend.py"),
                    str(archive),
                    "--backend",
                    "mast3r_slam",
                    "--work-dir",
                    str(work_dir),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertIn("Videos: 1", first.stdout)
            self.assertIn("work dir", first.stdout)
            self.assertIn("'", first.stdout)

            for path in source_root.iterdir():
                if path.is_dir():
                    for child in sorted(path.rglob("*"), reverse=True):
                        if child.is_file():
                            child.unlink()
                        elif child.is_dir():
                            child.rmdir()
                    path.rmdir()

            scan_dir = self._write_scan(source_root)
            self._zip_scan(scan_dir, archive, source_root)

            second = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "plan_neural_backend.py"),
                    str(archive),
                    "--backend",
                    "mast3r_slam",
                    "--work-dir",
                    str(work_dir),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            report_path = work_dir / "source" / "scan_test" / "metadata" / "mast3r_slam_neural_plan.json"
            payload = json.loads(report_path.read_text())

        self.assertIn("Videos: 0", second.stdout)
        self.assertEqual(payload["inputs"]["video_count"], 0)
        self.assertFalse((work_dir / "source" / "scan_test" / "video" / "scan.mp4").exists())

    def test_neural_backend_cli_does_not_delete_extracted_scan_when_work_dir_matches_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "plan_neural_backend.py"),
                    str(scan_dir),
                    "--backend",
                    "mast3r_slam",
                    "--work-dir",
                    str(scan_dir),
                ],
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must not be the scan directory", result.stderr)
            self.assertTrue((scan_dir / "images" / "frame_000001.jpg").exists())
            self.assertTrue((scan_dir / "metadata" / "frames.json").exists())

    def test_neural_backend_cli_rejects_input_inside_reset_source_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_dir = tmp_path / "work"
            source_dir = work_dir / "source"
            source_root = tmp_path / "source"
            scan_dir = self._write_scan(source_root)
            archive = source_dir / "scan_test.zip"
            source_dir.mkdir(parents=True)
            self._zip_scan(scan_dir, archive, source_root)

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "plan_neural_backend.py"),
                    str(archive),
                    "--backend",
                    "mast3r_slam",
                    "--work-dir",
                    str(work_dir),
                ],
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must not contain the input scan", result.stderr)
            self.assertTrue(archive.exists())

    def test_neural_backend_cli_rejects_extracted_input_inside_reset_source_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_dir = tmp_path / "work"
            scan_dir = self._write_scan(work_dir / "source")

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "plan_neural_backend.py"),
                    str(scan_dir),
                    "--backend",
                    "mast3r_slam",
                    "--work-dir",
                    str(work_dir),
                ],
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must not contain the input scan", result.stderr)
            self.assertTrue((scan_dir / "images" / "frame_000001.jpg").exists())

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

    def _zip_scan(self, scan_dir: Path, archive: Path, root: Path) -> None:
        if archive.exists():
            archive.unlink()
        with zipfile.ZipFile(archive, "w") as zip_file:
            for path in scan_dir.rglob("*"):
                if path.is_file():
                    zip_file.write(path, path.relative_to(root))


if __name__ == "__main__":
    unittest.main()
