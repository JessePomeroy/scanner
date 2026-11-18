from __future__ import annotations

import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "experiments"))

from app.scan_package import validate_and_report_scan  # noqa: E402
from scanner_lab.benchmark_evidence import (  # noqa: E402
    BenchmarkEvidenceError,
    append_stage,
    artifact_fact,
    ensure_report_open,
    initialize_report,
    probe_command,
    run_stage,
    runtime_guidance,
    sha256_file,
    verified_input,
)
from scanner_lab.gaussian_cleanup import (  # noqa: E402
    GaussianCleanupError,
    cleanup_gaussian_ply,
    load_gaussian_cleanup_recipe,
    read_gaussian_ply_header,
)
from scanner_lab.neural_backend_planner import (  # noqa: E402
    NERFSTUDIO_COLMAP_COMPAT_PATH,
    NeuralBackendConfig,
    SUPPORTED_SPLAT_DELIVERY_FORMATS,
    build_neural_backend_plan,
    write_neural_backend_report,
)
from scanner_lab.open3d_cleanup import cleanup_outputs  # noqa: E402
from scanner_lab.point_cloud_processor import (  # noqa: E402
    PointCloudProcessingConfig,
    build_processing_summary,
    process_point_cloud,
    write_processing_report,
)
from scanner_lab.reconstruction_backends import BackendPlanConfig, build_backend_plan  # noqa: E402
from scanner_lab.reconstruction_plan import write_command_plan_report  # noqa: E402
from scan_fixtures import ScanFixtureMixin  # noqa: E402


def setUpModule():
    from heavy_work_fixture import isolated_heavy_work
    unittest.enterModuleContext(isolated_heavy_work())


class ExperimentTests(ScanFixtureMixin, unittest.TestCase):
    """Engines, planners and evidence tools kept outside the everyday flow."""

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

    def test_backend_plan_exposes_explicit_colmap_fused_openmvs_path(self) -> None:
        scan_dir = Path("/tmp/scanner-colmap-fused-plan")
        plan = build_backend_plan(
            scan_dir,
            BackendPlanConfig(
                backend="colmap_openmvs",
                openmvs_point_cloud_source="colmap_fused",
                openmvs_scope_mode="unbounded",
            ),
        )

        openmvs_commands = [
            command
            for command in plan.commands
            if command[0]
            in {"InterfaceCOLMAP", "DensifyPointCloud", "ReconstructMesh", "TextureMesh"}
        ]
        self.assertEqual(
            [command[0] for command in openmvs_commands],
            ["InterfaceCOLMAP", "ReconstructMesh", "TextureMesh"],
        )
        for command in openmvs_commands:
            self.assertEqual(
                command[command.index("--working-folder") + 1],
                str(scan_dir.resolve() / "dense"),
            )
        self.assertEqual(
            plan.outputs["openmvs_dense_point_cloud"],
            scan_dir.resolve() / "dense" / "scene.ply",
        )
        self.assertEqual(
            plan.settings["openmvs"]["point_cloud_source"],
            "colmap_fused",
        )
        self.assertTrue(any("DensifyPointCloud is skipped" in note for note in plan.notes))

        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "plan.json"
            write_command_plan_report(plan, report_path)
            report = json.loads(report_path.read_text())
        self.assertEqual(
            report["settings"]["openmvs"]["point_cloud_source"],
            "colmap_fused",
        )

    def test_backend_plan_config_preserves_existing_positional_field_order(self) -> None:
        sensor_database = Path("/tmp/sensors.db")
        config = BackendPlanConfig(
            "alicevision",
            "sequential_matcher",
            False,
            False,
            False,
            "draft",
            sensor_database,
        )

        self.assertEqual(config.meshroom_pipeline, "draft")
        self.assertEqual(config.alicevision_sensor_database, sensor_database)
        self.assertEqual(config.openmvs_point_cloud_source, "openmvs_densify")

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

    def test_manifest_and_neural_plan_ignore_unsupported_image_files_consistently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            (scan_dir / "images" / "notes.txt").write_text("not a capture image")

            package = validate_and_report_scan(scan_dir)
            manifest = json.loads(package.manifest_path.read_text())
            plan = build_neural_backend_plan(
                scan_dir,
                NeuralBackendConfig(backend="mast3r_slam"),
            )

        self.assertEqual(package.validation.image_count, 1)
        self.assertEqual(manifest["file_counts"]["images"], 1)
        self.assertEqual(plan.inputs["image_count"], 1)

    def test_benchmark_runtime_guidance_matches_agreed_thresholds(self) -> None:
        self.assertEqual(runtime_guidance(None)["classification"], "uncalibrated")
        self.assertEqual(runtime_guidance(3 * 60 * 60)["classification"], "daytime")
        self.assertEqual(
            runtime_guidance(3 * 60 * 60 + 1)["classification"],
            "overnight_candidate",
        )
        limit = runtime_guidance(12 * 60 * 60)
        self.assertEqual(limit["classification"], "practical_limit_warning")
        self.assertTrue(limit["practical_limit_warning"])

    def test_benchmark_input_hash_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan = Path(tmp) / "scan.zip"
            scan.write_bytes(b"benchmark")
            expected = sha256_file(scan)
            facts = verified_input(scan, expected)
            self.assertEqual(facts["sha256"], expected)
            with self.assertRaisesRegex(BenchmarkEvidenceError, "mismatch"):
                verified_input(scan, "0" * 64)

    def test_benchmark_report_records_separate_baseline_and_tool_commits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan = Path(tmp) / "scan.zip"
            scan.write_bytes(b"benchmark")
            report = initialize_report(
                scan_path=scan,
                expected_sha256=sha256_file(scan),
                scanner_baseline_revision="HEAD",
                repo_root=ROOT,
                tool_probes={},
            )

        self.assertEqual(len(report["provenance"]["scanner_baseline_commit"]), 40)
        self.assertEqual(report["summary"]["status"], "initialized")
        self.assertEqual(report["provenance"]["tools"], {})

    def test_benchmark_probe_accepts_openmvs_help_banner_nonzero_exit(self) -> None:
        completed = SimpleNamespace(
            returncode=1,
            stdout="OpenMVS x64 v2.4.0\nAvailable options:\n",
        )
        with (
            patch("scanner_lab.benchmark_evidence.shutil.which", return_value="/usr/bin/InterfaceCOLMAP"),
            patch("scanner_lab.benchmark_evidence.subprocess.run", return_value=completed),
        ):
            result = probe_command(["InterfaceCOLMAP", "--help"])

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["return_code"], 1)
        self.assertTrue(result["accepted_nonzero_exit"])

    def test_benchmark_probe_rejects_unrecognized_nonzero_help_output(self) -> None:
        completed = SimpleNamespace(returncode=1, stdout="unexpected failure\n")
        with (
            patch("scanner_lab.benchmark_evidence.shutil.which", return_value="/usr/bin/InterfaceCOLMAP"),
            patch("scanner_lab.benchmark_evidence.subprocess.run", return_value=completed),
        ):
            result = probe_command(["InterfaceCOLMAP", "--help"])

        self.assertEqual(result["status"], "error")
        self.assertFalse(result["accepted_nonzero_exit"])

    def test_benchmark_probe_uses_disposable_working_directory(self) -> None:
        observed_cwd: Path | None = None

        def fake_run(*args: object, **kwargs: object) -> SimpleNamespace:
            nonlocal observed_cwd
            observed_cwd = Path(str(kwargs["cwd"]))
            self.assertTrue(observed_cwd.is_dir())
            return SimpleNamespace(returncode=0, stdout="tool 1.0\n")

        with (
            patch("scanner_lab.benchmark_evidence.shutil.which", return_value="/usr/bin/tool"),
            patch("scanner_lab.benchmark_evidence.subprocess.run", side_effect=fake_run),
        ):
            result = probe_command(["tool", "--version"])

        self.assertEqual(result["status"], "ok")
        self.assertIsNotNone(observed_cwd)
        assert observed_cwd is not None
        self.assertFalse(observed_cwd.exists())

    def test_benchmark_stage_records_log_time_vram_and_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            success = run_stage(
                name="success",
                command=[sys.executable, "-c", "print('stage output')"],
                log_path=tmp_path / "success.log",
                poll_interval_seconds=0.01,
                gpu_sampler=lambda: 321,
            )
            failed = run_stage(
                name="failure",
                command=[sys.executable, "-c", "raise SystemExit(7)"],
                log_path=tmp_path / "failure.log",
                poll_interval_seconds=0.01,
                gpu_sampler=lambda: None,
            )

        self.assertEqual(success["status"], "succeeded")
        self.assertEqual(success["peak_vram_mib"], 321)
        self.assertGreater(success["elapsed_seconds"], 0)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["return_code"], 7)

    def test_benchmark_vram_sampling_failure_does_not_kill_stage(self) -> None:
        def broken_sampler() -> int | None:
            raise RuntimeError("nvidia-smi unavailable during sample")

        with tempfile.TemporaryDirectory() as tmp:
            stage = run_stage(
                name="sampling_failure",
                command=[sys.executable, "-c", "print('still runs')"],
                log_path=Path(tmp) / "sampling_failure.log",
                poll_interval_seconds=0.01,
                gpu_sampler=broken_sampler,
            )

        self.assertEqual(stage["status"], "succeeded")
        self.assertIsNone(stage["peak_vram_mib"])
        self.assertGreater(stage["vram_sample_errors"], 0)

    def test_benchmark_stage_names_cannot_overwrite_evidence(self) -> None:
        report = {
            "stages": [],
            "updated_at": None,
            "summary": {},
        }
        stage = {
            "name": "mesh",
            "status": "planned",
            "elapsed_seconds": 0,
            "peak_vram_mib": None,
        }
        append_stage(report, stage)
        with self.assertRaisesRegex(BenchmarkEvidenceError, "already exists"):
            append_stage(report, stage)

    def test_benchmark_terminal_reports_cannot_be_mutated(self) -> None:
        for status in ("complete", "failed"):
            with self.subTest(status=status):
                with self.assertRaisesRegex(BenchmarkEvidenceError, "terminal"):
                    ensure_report_open({"summary": {"status": status}})

        ensure_report_open({"summary": {"status": "in_progress"}})

    def test_benchmark_artifact_fact_hashes_files_and_sizes_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "scene.sog"
            artifact.write_bytes(b"sog")
            nested = root / "textures"
            nested.mkdir()
            (nested / "texture.jpg").write_bytes(b"image")
            file_fact = artifact_fact(artifact)
            directory_fact = artifact_fact(nested)

        self.assertEqual(file_fact["size_bytes"], 3)
        self.assertEqual(
            file_fact["sha256"],
            "3b5d25db043a9eeb5b15c4684208ba1ab433aca45b7886d7bbcf29be4275285b",
        )
        self.assertEqual(directory_fact["file_count"], 1)
        self.assertEqual(directory_fact["size_bytes"], 5)

    def test_benchmark_evidence_cli_round_trips_a_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scan = root / "scan.zip"
            scan.write_bytes(b"benchmark")
            artifact = root / "scene.sog"
            artifact.write_bytes(b"sog")
            report_path = root / "evidence.json"
            script = ROOT / "experiments" / "scripts" / "benchmark_evidence.py"

            subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "init",
                    "--scan",
                    str(scan),
                    "--expected-sha256",
                    sha256_file(scan),
                    "--scanner-baseline-commit",
                    "HEAD",
                    "--report",
                    str(report_path),
                    "--skip-tool-probes",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "run",
                    "--report",
                    str(report_path),
                    "--stage",
                    "mesh_plan",
                    "--dry-run",
                    "--",
                    "colmap",
                    "feature_extractor",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "finalize",
                    "--report",
                    str(report_path),
                    "--artifact",
                    f"splat_sog={artifact}",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(report_path.read_text())

        self.assertEqual(payload["summary"]["status"], "complete")
        self.assertEqual(payload["schedule"]["classification"], "uncalibrated")
        self.assertEqual(payload["stages"][0]["status"], "planned")
        self.assertEqual(payload["artifacts"]["splat_sog"]["size_bytes"], 3)

    def test_neural_backend_plan_prefers_video_for_mast3r_slam(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            video_dir = scan_dir / "video"
            video_dir.mkdir()
            (video_dir / "scan.mp4").write_bytes(b"fake mp4")
            (scan_dir / "metadata" / "video.json").write_text(
                json.dumps([self._video_metadata(path="video/scan.mp4")])
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

    def test_gaussian_cleanup_filters_ascii_crop_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "splat.ply"
            output = root / "splat.cleaned.ply"
            report = root / "cleanup.json"
            recipe_path = root / "recipe.json"
            source.write_text(
                "ply\nformat ascii 1.0\nelement vertex 5\n"
                "property float x\nproperty float y\nproperty float z\n"
                "property float opacity\nend_header\n"
                "-2 0 0 0.1\n-1 0 0 0.2\n0 0 0 0.3\n1 0 0 0.4\n2 0 0 0.5\n"
            )
            source_before = source.read_bytes()
            recipe_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "revision": 2,
                        "crop": {
                            "shape": "box",
                            "center": [0, 0, 0],
                            "size": [2, 2, 2],
                            "keep": "inside",
                        },
                    }
                )
            )

            cleanup_gaussian_ply(
                source,
                output,
                load_gaussian_cleanup_recipe(recipe_path),
                report_path=report,
            )
            header = read_gaussian_ply_header(output, primitive_hard_limit=100)
            evidence = json.loads(report.read_text())

            self.assertEqual(source.read_bytes(), source_before)
            self.assertEqual(header.vertex_count, 3)
            self.assertIn(b"-1 0 0 0.2", output.read_bytes())
            self.assertEqual(evidence["source_primitive_count"], 5)
            self.assertEqual(evidence["retained_primitive_count"], 3)
            self.assertEqual(evidence["artifact_type"], "gaussian_ply")
            self.assertEqual(evidence["cleanup_revision"], 2)
            self.assertEqual(evidence["effective_bounds"]["shape"], "box")
            self.assertTrue(evidence["destructive_output_verified"])

            cleanup_gaussian_ply(
                source,
                output,
                load_gaussian_cleanup_recipe(recipe_path),
                report_path=report,
                overwrite=True,
            )
            self.assertEqual(
                read_gaussian_ply_header(output, primitive_hard_limit=100).vertex_count,
                3,
            )

    def test_gaussian_cleanup_filters_binary_selection_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "splat.ply"
            output = root / "splat.cleaned.ply"
            report = root / "cleanup.json"
            recipe_path = root / "recipe.json"
            header = (
                b"ply\nformat binary_little_endian 1.0\nelement vertex 4\n"
                b"property float x\nproperty float y\nproperty float z\n"
                b"property float opacity\nend_header\n"
            )
            records = b"".join(
                struct.pack("<ffff", float(index), 0, 0, index / 10)
                for index in range(4)
            )
            source.write_bytes(header + records)
            recipe_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "revision": 1,
                        "selection": {"mode": "discard", "ranges": [[1, 3]]},
                    }
                )
            )

            cleanup_gaussian_ply(
                source,
                output,
                load_gaussian_cleanup_recipe(recipe_path),
                report_path=report,
            )
            output_header = read_gaussian_ply_header(output, primitive_hard_limit=100)
            payload = output.read_bytes()[output_header.header_size :]

            self.assertEqual(output_header.vertex_count, 2)
            self.assertEqual(len(payload), struct.calcsize("<ffff") * 2)
            self.assertEqual(struct.unpack("<ffff", payload[:16])[0], 0)
            self.assertEqual(struct.unpack("<ffff", payload[16:])[0], 3)

    def test_gaussian_cleanup_rejects_out_of_bounds_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "splat.ply"
            source.write_text(
                "ply\nformat ascii 1.0\nelement vertex 1\n"
                "property float x\nproperty float y\nproperty float z\n"
                "end_header\n0 0 0\n"
            )
            recipe_path = root / "recipe.json"
            recipe_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "revision": 1,
                        "selection": {"mode": "keep", "ranges": [[0, 2]]},
                    }
                )
            )

            with self.assertRaises(GaussianCleanupError):
                cleanup_gaussian_ply(
                    source,
                    root / "cleaned.ply",
                    load_gaussian_cleanup_recipe(recipe_path),
                    report_path=root / "report.json",
                )

    def test_gaussian_cleanup_overwrite_refuses_hardlink_to_master(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "splat.ply"
            source.write_text(
                "ply\nformat ascii 1.0\nelement vertex 1\n"
                "property float x\nproperty float y\nproperty float z\n"
                "end_header\n0 0 0\n"
            )
            output = root / "alias.ply"
            os.link(source, output)
            recipe_path = root / "recipe.json"
            recipe_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "revision": 1,
                        "crop": {
                            "shape": "box",
                            "center": [0, 0, 0],
                            "size": [2, 2, 2],
                            "keep": "inside",
                        },
                    }
                )
            )

            with self.assertRaisesRegex(GaussianCleanupError, "immutable source"):
                cleanup_gaussian_ply(
                    source,
                    output,
                    load_gaussian_cleanup_recipe(recipe_path),
                    report_path=root / "report.json",
                    overwrite=True,
                )
            self.assertIn(b"element vertex 1", source.read_bytes())

    def test_neural_backend_plan_uses_images_for_gaussian_splatting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            plan = build_neural_backend_plan(
                scan_dir,
                NeuralBackendConfig(backend="gaussian_splatting"),
            )

        self.assertEqual(plan.backend, "gaussian_splatting")
        self.assertEqual(plan.inputs["preferred_source_type"], "images")
        self.assertEqual(plan.commands[0][0:2], ["ns-process-data", "images"])
        self.assertIn("--matching-method", plan.commands[0])
        self.assertEqual(
            plan.commands[0][plan.commands[0].index("--colmap-cmd") + 1],
            str(NERFSTUDIO_COLMAP_COMPAT_PATH),
        )
        self.assertTrue(NERFSTUDIO_COLMAP_COMPAT_PATH.is_absolute())
        self.assertEqual(
            plan.commands[0][plan.commands[0].index("--sfm-tool") + 1],
            "colmap",
        )
        self.assertEqual(plan.commands[1][0:2], ["ns-train", "splatfacto"])
        self.assertEqual(
            plan.commands[1][
                plan.commands[1].index("--viewer.quit-on-train-completion") + 1
            ],
            "True",
        )
        self.assertEqual(
            plan.commands[1][plan.commands[1].index("--viewer.websocket-host") + 1],
            "127.0.0.1",
        )
        self.assertEqual(plan.commands[2][0:2], ["ns-export", "gaussian-splat"])
        self.assertEqual(plan.inputs["delivery_formats"], ["sog", "html"])
        self.assertTrue(str(plan.outputs["splat_ply"]).endswith("exports/splat/splat.ply"))
        self.assertTrue(str(plan.outputs["splat_sog"]).endswith("delivery/scene.sog"))
        self.assertTrue(
            str(plan.outputs["splat_html_viewer"]).endswith("delivery/scene.html")
        )
        self.assertEqual(plan.commands[3][0:2], ["splat-transform", "--overwrite"])
        self.assertIn("--filter-nan", plan.commands[3])
        self.assertTrue(plan.commands[3][-1].endswith("scene.sog"))
        self.assertTrue(plan.commands[4][-1].endswith("scene.html"))

    def test_neural_backend_plan_converts_only_verified_cleaned_splat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scan_dir = self._write_scan(root)
            recipe = root / "gaussian_cleanup.json"
            recipe.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "revision": 4,
                        "crop": {
                            "shape": "box",
                            "center": [0, 0, 0],
                            "size": [2, 2, 2],
                            "keep": "inside",
                        },
                    }
                )
            )
            plan = build_neural_backend_plan(
                scan_dir,
                NeuralBackendConfig(
                    backend="gaussian_splatting",
                    splat_cleanup_recipe=recipe,
                ),
            )

        cleanup = plan.commands[3]
        cleaned_ply = str(plan.outputs["splat_cleaned_ply"])
        self.assertEqual(cleanup[0], "python3")
        self.assertTrue(cleanup[1].endswith("experiments/scripts/cleanup_gaussian_ply.py"))
        self.assertEqual(cleanup[3], cleaned_ply)
        self.assertEqual(plan.commands[4][2], cleaned_ply)
        self.assertEqual(plan.commands[5][2], cleaned_ply)
        self.assertIn("splat_cleanup_report", plan.outputs)

    def test_neural_backend_plan_supports_selected_splat_delivery_formats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            plan = build_neural_backend_plan(
                scan_dir,
                NeuralBackendConfig(
                    backend="gaussian_splatting",
                    splat_delivery_formats=(
                        "compressed-ply",
                        "spz",
                        "gaussian-glb",
                        "spz",
                    ),
                ),
            )

        self.assertEqual(
            plan.inputs["delivery_formats"],
            ["compressed-ply", "spz", "gaussian-glb"],
        )
        self.assertEqual(len(plan.commands), 6)
        self.assertTrue(plan.commands[3][-1].endswith("scene.compressed.ply"))
        self.assertTrue(plan.commands[4][-1].endswith("scene.spz"))
        self.assertTrue(plan.commands[5][-1].endswith("scene.gaussian.glb"))
        self.assertIn("splat_gaussian_glb", plan.outputs)

    def test_neural_backend_plan_rejects_unknown_splat_delivery_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            with self.assertRaisesRegex(ValueError, "Unsupported splat delivery format"):
                build_neural_backend_plan(
                    scan_dir,
                    NeuralBackendConfig(
                        backend="gaussian_splatting",
                        splat_delivery_formats=("made-up",),
                    ),
                )

        self.assertIn("sog", SUPPORTED_SPLAT_DELIVERY_FORMATS)

    def test_neural_backend_plan_prefers_images_over_video_for_gaussian_splatting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            video_dir = scan_dir / "video"
            video_dir.mkdir()
            (video_dir / "scan.mov").write_bytes(b"fake mov")
            (scan_dir / "metadata" / "video.json").write_text(
                json.dumps([self._video_metadata()])
            )

            plan = build_neural_backend_plan(
                scan_dir,
                NeuralBackendConfig(backend="gaussian_splatting"),
            )

        self.assertEqual(plan.inputs["preferred_source_type"], "images")
        self.assertEqual(plan.commands[0][0:2], ["ns-process-data", "images"])
        self.assertFalse(any("video/scan.mov" in part for part in plan.commands[0]))

    def test_neural_backend_plan_uses_video_when_images_are_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            (scan_dir / "images" / "frame_000001.jpg").unlink()
            video_dir = scan_dir / "video"
            video_dir.mkdir()
            (video_dir / "scan.mov").write_bytes(b"fake mov")
            (scan_dir / "metadata" / "video.json").write_text(
                json.dumps([self._video_metadata()])
            )

            plan = build_neural_backend_plan(
                scan_dir,
                NeuralBackendConfig(backend="gaussian_splatting"),
            )

        self.assertEqual(plan.inputs["preferred_source_type"], "video")
        self.assertTrue(any("video/scan.mov" in part for part in plan.commands[0]))

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

    def test_neural_backend_cli_refuses_stale_zip_workspace(self) -> None:
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
                json.dumps([self._video_metadata(path="video/scan.mp4")])
            )
            self._zip_scan(scan_dir, archive, source_root)

            first = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "experiments" / "scripts" / "plan_neural_backend.py"),
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
                    str(ROOT / "experiments" / "scripts" / "plan_neural_backend.py"),
                    str(archive),
                    "--backend",
                    "mast3r_slam",
                    "--work-dir",
                    str(work_dir),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            report_path = work_dir / "source" / "scan_test" / "metadata" / "mast3r_slam_neural_plan.json"
            payload = json.loads(report_path.read_text())

            self.assertNotEqual(second.returncode, 0)
            self.assertIn("choose a fresh workspace", second.stderr)
            self.assertEqual(payload["inputs"]["video_count"], 1)
            self.assertEqual(
                (work_dir / "source" / "scan_test" / "video" / "scan.mp4").read_bytes(),
                b"fake mp4",
            )

    def test_neural_backend_cli_wires_gaussian_splat_flags_to_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_root = tmp_path / "source"
            archive = tmp_path / "scan_test.zip"
            work_dir = tmp_path / "gaussian work"

            scan_dir = self._write_scan(source_root)
            self._zip_scan(scan_dir, archive, source_root)

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "experiments" / "scripts" / "plan_neural_backend.py"),
                    str(archive),
                    "--backend",
                    "gaussian_splatting",
                    "--work-dir",
                    str(work_dir),
                    "--splat-method",
                    "splatfacto-big",
                    "--splat-matching-method",
                    "exhaustive",
                    "--splat-delivery-format",
                    "spz",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            report_path = (
                work_dir
                / "source"
                / "scan_test"
                / "metadata"
                / "gaussian_splatting_neural_plan.json"
            )
            payload = json.loads(report_path.read_text())

        self.assertIn("Backend: gaussian_splatting", result.stdout)
        self.assertIn("splatfacto-big", payload["commands"][1])
        self.assertIn("exhaustive", payload["commands"][0])
        wrapper_index = payload["commands"][0].index("--colmap-cmd") + 1
        self.assertEqual(
            payload["commands"][0][wrapper_index],
            str(ROOT / "scripts" / "nerfstudio_colmap_compat.py"),
        )
        self.assertIn("--viewer.quit-on-train-completion", payload["commands"][1])
        self.assertIn("--viewer.websocket-host", payload["commands"][1])
        self.assertEqual(payload["inputs"]["preferred_source_type"], "images")
        self.assertEqual(payload["inputs"]["delivery_formats"], ["spz"])
        self.assertTrue(payload["commands"][3][-1].endswith("scene.spz"))
        self.assertIn("splat_spz", payload["outputs"])

    def test_neural_backend_cli_does_not_delete_extracted_scan_when_work_dir_matches_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "experiments" / "scripts" / "plan_neural_backend.py"),
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
                    str(ROOT / "experiments" / "scripts" / "plan_neural_backend.py"),
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
                    str(ROOT / "experiments" / "scripts" / "plan_neural_backend.py"),
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


if __name__ == "__main__":
    unittest.main()
