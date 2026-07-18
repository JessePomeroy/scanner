from __future__ import annotations

import asyncio
from io import BytesIO
import json
import importlib.util
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

import numpy as np
from PIL import Image

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

BLENDER_SCRIPT = ROOT / "scripts" / "blender" / "prepare_scan_asset.py"

from app.artifacts import (  # noqa: E402
    ArtifactUnavailableError,
    UnsafeArtifactPathError,
    list_downloadable_artifacts,
    open_downloadable_artifact,
    rebase_output_paths,
)
from app.benchmark_evidence import (  # noqa: E402
    BenchmarkEvidenceError,
    append_stage,
    artifact_fact,
    ensure_report_open,
    initialize_report,
    run_stage,
    runtime_guidance,
    sha256_file,
    verified_input,
)
from app.colmap_runner import (  # noqa: E402
    build_colmap_commands,
    build_colmap_dense_commands,
    build_colmap_sparse_commands,
)
from app.density_budget import PointCloudBudgetError, inspect_ply_point_budget  # noqa: E402
from app.job_recovery import reconcile_interrupted_jobs  # noqa: E402
from app.mask_processor import MaskValidationError, validate_openmvs_masks  # noqa: E402
from app.mask_undistorter import (  # noqa: E402
    ColmapCamera,
    MaskCameraAssociation,
    MaskUndistortionError,
    parse_colmap_cameras,
    parse_colmap_image_cameras,
    associate_mask_cameras,
    convert_capture_mask_set,
    undistort_mask_array,
    undistort_mask_file,
)
from app.jobs import JobStore, JobTransitionError  # noqa: E402
from app.neural_backend_planner import (  # noqa: E402
    NeuralBackendConfig,
    SUPPORTED_SPLAT_DELIVERY_FORMATS,
    build_neural_backend_plan,
    write_neural_backend_report,
)
from app.openmvs_runner import OpenMVSConfig, build_openmvs_commands, run_openmvs_pipeline  # noqa: E402
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
from app.scan_validator import (  # noqa: E402
    ScanValidationError,
    find_scan_root,
    validate_scan_package,
)
from app.storage import (  # noqa: E402
    UnsafeArchiveError,
    safe_extract_zip,
    store_upload_atomically,
)
from app.upload_lifecycle import store_job_upload  # noqa: E402


def _png_header(width: int, height: int, *, color_type: int) -> bytes:
    """Return the bytes needed by header-only PNG dimension tests."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\x0dIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + bytes((8, color_type, 0, 0, 0))
        + b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _grayscale_png(width: int, height: int, *, value: int = 255) -> bytes:
    output = BytesIO()
    Image.new("L", (width, height), color=value).save(output, format="PNG")
    return output.getvalue()


class RecordingAsyncReader:
    def __init__(
        self,
        payload: bytes,
        *,
        fail_on_call: int | None = None,
        failure: BaseException | None = None,
    ) -> None:
        self.payload = payload
        self.offset = 0
        self.fail_on_call = fail_on_call
        self.failure = failure
        self.read_sizes: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if self.fail_on_call == len(self.read_sizes):
            raise self.failure or OSError("simulated upload read failure")
        if self.offset >= len(self.payload):
            return b""

        end = len(self.payload) if size < 0 else self.offset + size
        chunk = self.payload[self.offset:end]
        self.offset += len(chunk)
        return chunk


class PausingAsyncReader:
    def __init__(
        self,
        payload: bytes,
        *,
        started: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self.payload = payload
        self.started = started
        self.release = release
        self.did_read = False

    async def read(self, size: int = -1) -> bytes:
        if self.did_read:
            return b""
        self.did_read = True
        self.started.set()
        await self.release.wait()
        return self.payload if size < 0 else self.payload[:size]


class RecordingUploadJobStore:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.updates: list[tuple[str, str, str]] = []

    def update(self, scan_id: str, *, status: str, message: str) -> object:
        self.updates.append((scan_id, status, message))
        if self.failure is not None:
            raise self.failure
        return object()


def load_blender_script_module():
    spec = importlib.util.spec_from_file_location("prepare_scan_asset", BLENDER_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load prepare_scan_asset.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class BackendTests(unittest.TestCase):
    def test_list_downloadable_artifacts_exposes_only_owned_regular_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            completed = root / "completed"
            failed = root / "failed"
            package = completed / "job-1"
            report = package / "metadata" / "scan_report.json"
            mesh = package / "dense" / "scene.obj"
            report.parent.mkdir(parents=True)
            mesh.parent.mkdir()
            failed.mkdir()
            report.write_text("{}")
            mesh.write_bytes(b"mesh")
            external = root / "outside.ply"
            external.write_bytes(b"outside")
            (package / "metadata" / "report-link.json").symlink_to(report)
            hardlink = package / "dense" / "hardlinked.ply"
            os.link(external, hardlink)

            artifacts = list_downloadable_artifacts(
                {
                    "package_dir": str(package),
                    "scan_report": str(report),
                    "textured_mesh": "dense/scene.obj",
                    "duplicate_mesh": str(mesh),
                    "output_directory": str(mesh.parent),
                    "external": str(external),
                    "missing": str(package / "missing.ply"),
                    "symlink": str(package / "metadata" / "report-link.json"),
                    "hardlink": str(hardlink),
                },
                allowed_package_roots=(completed, failed),
            )

        self.assertEqual(
            [(item.name, item.relative_path) for item in artifacts],
            [
                ("duplicate_mesh", "dense/scene.obj"),
                ("scan_report", "metadata/scan_report.json"),
            ],
        )
        self.assertEqual(artifacts[0].byte_count, 4)
        self.assertEqual(artifacts[0].filename, "scene.obj")
        self.assertTrue(artifacts[0].media_type)

    def test_resolve_downloadable_artifact_rejects_unsafe_or_missing_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            completed = root / "completed"
            failed = root / "failed"
            package = completed / "job-1"
            report = package / "metadata" / "scan_report.json"
            report.parent.mkdir(parents=True)
            failed.mkdir()
            report.write_text("{}")
            undeclared = package / "images" / "frame.jpg"
            undeclared.parent.mkdir()
            undeclared.write_bytes(b"image")
            (package / "metadata" / "report-link.json").symlink_to(report)
            outputs = {
                "package_dir": str(package),
                "scan_report": str(report),
            }

            opened = open_downloadable_artifact(
                outputs,
                "metadata/scan_report.json",
                allowed_package_roots=(completed, failed),
            )
            try:
                self.assertEqual(opened.file.read(), b"{}")
                self.assertEqual(opened.descriptor.relative_path, "metadata/scan_report.json")
            finally:
                opened.file.close()

            for unsafe_path in [
                "",
                "/etc/passwd",
                "..\\outside.ply",
                "../outside.ply",
                "metadata/../metadata/scan_report.json",
                "metadata/report-link.json",
            ]:
                with self.subTest(path=unsafe_path), self.assertRaises(
                    UnsafeArtifactPathError
                ):
                    open_downloadable_artifact(
                        outputs,
                        unsafe_path,
                        allowed_package_roots=(completed, failed),
                    )

            with self.assertRaises(ArtifactUnavailableError):
                open_downloadable_artifact(
                    outputs,
                    "metadata/missing.json",
                    allowed_package_roots=(completed, failed),
                )
            with self.assertRaises(ArtifactUnavailableError):
                open_downloadable_artifact(
                    outputs,
                    "images/frame.jpg",
                    allowed_package_roots=(completed, failed),
                )

    def test_open_downloadable_artifact_holds_authorized_inode_across_path_swap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            completed = root / "completed"
            failed = root / "failed"
            package = completed / "job-1"
            artifact_path = package / "dense" / "mesh.ply"
            artifact_path.parent.mkdir(parents=True)
            failed.mkdir()
            artifact_path.write_bytes(b"authorized mesh")
            external = root / "secret.txt"
            external.write_bytes(b"external secret")
            outputs = {
                "package_dir": str(package),
                "colmap_output": str(artifact_path),
            }

            opened = open_downloadable_artifact(
                outputs,
                "dense/mesh.ply",
                allowed_package_roots=(completed, failed),
            )
            artifact_path.unlink()
            artifact_path.symlink_to(external)
            try:
                self.assertEqual(opened.file.read(), b"authorized mesh")
            finally:
                opened.file.close()

    def test_hardlinked_output_is_not_published_or_downloadable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            completed = root / "completed"
            failed = root / "failed"
            package = completed / "job-1"
            artifact_path = package / "dense" / "mesh.ply"
            artifact_path.parent.mkdir(parents=True)
            failed.mkdir()
            external = root / "external.ply"
            external.write_bytes(b"external")
            os.link(external, artifact_path)
            outputs = {
                "package_dir": str(package),
                "colmap_output": str(artifact_path),
            }

            self.assertEqual(
                list_downloadable_artifacts(
                    outputs,
                    allowed_package_roots=(completed, failed),
                ),
                [],
            )
            with self.assertRaises(ArtifactUnavailableError):
                open_downloadable_artifact(
                    outputs,
                    "dense/mesh.ply",
                    allowed_package_roots=(completed, failed),
                )

    def test_artifact_package_directory_must_belong_to_scanner_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            completed = root / "completed"
            failed = root / "failed"
            completed.mkdir()
            failed.mkdir()
            external = root / "external"
            external.mkdir()

            with self.assertRaises(UnsafeArtifactPathError):
                list_downloadable_artifacts(
                    {"package_dir": str(external)},
                    allowed_package_roots=(completed, failed),
                )
            with self.assertRaises(ArtifactUnavailableError):
                list_downloadable_artifacts(
                    {},
                    allowed_package_roots=(completed, failed),
                )

    def test_rebase_output_paths_preserves_owned_relative_locations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            processing = root / "processing" / "job-1"
            completed = root / "completed" / "job-1"
            output = processing / "dense" / "scene.obj"
            output.parent.mkdir(parents=True)
            output.write_bytes(b"mesh")

            rebased = rebase_output_paths(
                {
                    "textured_mesh": str(output),
                    "relative_mesh": "dense/scene.obj",
                },
                old_root=processing,
                new_root=completed,
            )
            self.assertEqual(
                rebased,
                {
                    "textured_mesh": str(completed / "dense" / "scene.obj"),
                    "relative_mesh": str(completed / "dense" / "scene.obj"),
                },
            )

            previous_directory = Path.cwd()
            os.chdir(root)
            try:
                relative_rebased = rebase_output_paths(
                    {"relative_mesh": "dense/scene.obj"},
                    old_root=Path("processing/job-1"),
                    new_root=Path("completed/job-1"),
                )
            finally:
                os.chdir(previous_directory)
            self.assertEqual(
                relative_rebased,
                {"relative_mesh": "completed/job-1/dense/scene.obj"},
            )

            with self.assertRaises(UnsafeArtifactPathError):
                rebase_output_paths(
                    {"outside": str(root / "outside.ply")},
                    old_root=processing,
                    new_root=completed,
                )

    def test_validate_scan_package_accepts_valid_minimal_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            report = validate_scan_package(scan_dir)

        self.assertEqual(report.image_count, 1)
        self.assertEqual(report.frame_count, 1)
        self.assertEqual(report.video_count, 0)
        self.assertEqual(report.scan_id, "scan_test")
        self.assertEqual(report.scan_mode, "scene_scan")

    def test_validate_scan_package_preserves_typed_reconstruction_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            scope = {
                "schema_version": "1.0",
                "mode": "image_masks",
                "mask_space": "capture_image",
                "mask_convention": "white_keep_black_exclude",
                "mask_count": 1,
            }
            (scan_dir / "metadata" / "manifest.json").write_text(
                json.dumps({"reconstruction_scope": scope})
            )
            capture_masks = scan_dir / "masks" / "capture"
            capture_masks.mkdir(parents=True)
            (capture_masks / "frame_000001.jpg.png").write_bytes(
                _grayscale_png(1920, 1080)
            )

            package = validate_and_report_scan(scan_dir)
            generated_manifest = json.loads(package.manifest_path.read_text())

        self.assertEqual(package.validation.reconstruction_scope, scope)
        self.assertEqual(generated_manifest["reconstruction_scope"], scope)
        self.assertEqual(generated_manifest["file_counts"]["capture_masks"], 1)

    def test_validate_scan_package_preserves_post_capture_mask_authoring(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            authoring = {
                "schema_version": "1.0",
                "authoring_mode": "representative_frames",
                "coordinate_space": "normalized_capture_image",
                "mask_convention": "white_keep_black_exclude",
                "revision": 1,
                "representative_frames": [{
                    "frame_id": 1,
                    "image": "images/frame_000001.jpg",
                    "regions": [{
                        "operation": "keep",
                        "points": [
                            {"x": 0.1, "y": 0.1}, {"x": 0.9, "y": 0.1},
                            {"x": 0.9, "y": 0.9},
                        ],
                    }],
                }],
            }
            (scan_dir / "metadata" / "mask_authoring.json").write_text(json.dumps(authoring))

            package = validate_and_report_scan(scan_dir)
            generated_manifest = json.loads(package.manifest_path.read_text())

        self.assertEqual(package.validation.mask_authoring, authoring)
        self.assertEqual(generated_manifest["mask_authoring"], authoring)

    def test_validate_scan_package_rejects_corrupt_capture_mask_pixel_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            scope = {
                "schema_version": "1.0",
                "mode": "image_masks",
                "mask_space": "capture_image",
                "mask_convention": "white_keep_black_exclude",
                "mask_count": 1,
            }
            (scan_dir / "metadata" / "manifest.json").write_text(
                json.dumps({"reconstruction_scope": scope})
            )
            capture_masks = scan_dir / "masks" / "capture"
            capture_masks.mkdir(parents=True)
            (capture_masks / "frame_000001.jpg.png").write_bytes(
                _png_header(1920, 1080, color_type=0)
            )

            with self.assertRaisesRegex(ScanValidationError, "Unable to decode PNG mask"):
                validate_scan_package(scan_dir)

    def test_validate_scan_package_rejects_png_decompression_bomb(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            scope = {
                "schema_version": "1.0",
                "mode": "image_masks",
                "mask_space": "capture_image",
                "mask_convention": "white_keep_black_exclude",
                "mask_count": 1,
            }
            (scan_dir / "metadata" / "manifest.json").write_text(
                json.dumps({"reconstruction_scope": scope})
            )
            capture_masks = scan_dir / "masks" / "capture"
            capture_masks.mkdir(parents=True)
            (capture_masks / "frame_000001.jpg.png").write_bytes(
                _grayscale_png(1920, 1080)
            )

            with (
                patch(
                    "app.mask_processor.Image.open",
                    side_effect=Image.DecompressionBombError("unsafe dimensions"),
                ),
                self.assertRaisesRegex(ScanValidationError, "Unable to decode PNG mask"),
            ):
                validate_scan_package(scan_dir)

    def test_validate_scan_package_rejects_invalid_reconstruction_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            (scan_dir / "metadata" / "manifest.json").write_text(
                json.dumps(
                    {
                        "reconstruction_scope": {
                            "schema_version": "1.0",
                            "mode": "unbounded",
                            "mask_space": "capture_image",
                            "mask_convention": "white_keep_black_exclude",
                            "mask_count": 1,
                        }
                    }
                )
            )

            with self.assertRaises(ScanValidationError):
                validate_scan_package(scan_dir)

    def test_validate_scan_package_rejects_masks_without_scope_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            capture_masks = scan_dir / "masks" / "capture"
            capture_masks.mkdir(parents=True)
            (capture_masks / "frame_000001.jpg.png").write_bytes(
                _png_header(1920, 1080, color_type=0)
            )

            with self.assertRaises(ScanValidationError):
                validate_scan_package(scan_dir)

    def test_validate_scan_package_rejects_capture_mask_count_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            (scan_dir / "metadata" / "manifest.json").write_text(
                json.dumps(
                    {
                        "reconstruction_scope": {
                            "schema_version": "1.0",
                            "mode": "image_masks",
                            "mask_space": "capture_image",
                            "mask_convention": "white_keep_black_exclude",
                            "mask_count": 1,
                        }
                    }
                )
            )
            (scan_dir / "masks" / "capture").mkdir(parents=True)

            with self.assertRaises(ScanValidationError):
                validate_scan_package(scan_dir)

    def test_validate_scan_package_rejects_missing_image_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            (scan_dir / "images" / "frame_000001.jpg").unlink()

            with self.assertRaises(ScanValidationError):
                validate_scan_package(scan_dir)

    def test_find_scan_root_skips_child_directory_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            external_scan = self._write_scan(root / "external")
            extracted_dir = root / "extracted"
            extracted_dir.mkdir()
            (extracted_dir / "scan_link").symlink_to(external_scan, target_is_directory=True)

            scan_root = find_scan_root(extracted_dir)

            self.assertEqual(scan_root, extracted_dir.resolve())
            with self.assertRaises(ScanValidationError):
                validate_and_report_scan(scan_root)
            self.assertFalse((external_scan / "metadata" / "manifest.json").exists())
            self.assertFalse((external_scan / "metadata" / "scan_report.json").exists())

    def test_validate_scan_package_accepts_optional_video_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            video_dir = scan_dir / "video"
            video_dir.mkdir()
            (video_dir / "scan.mov").write_bytes(b"fake mov")
            (scan_dir / "metadata" / "video.json").write_text(
                json.dumps([self._video_metadata()])
            )
            session_path = scan_dir / "metadata" / "session.json"
            session = json.loads(session_path.read_text())
            session.update({"image_count": 1, "video_count": 1})
            session_path.write_text(json.dumps(session))

            report = validate_scan_package(scan_dir)

        self.assertEqual(report.video_count, 1)
        self.assertEqual(report.video_metadata_count, 1)
        self.assertEqual(report.session_image_count, 1)
        self.assertEqual(report.session_video_count, 1)
        self.assertEqual(report.integrity_warnings, ())

    def test_validate_scan_package_rejects_missing_video_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            (scan_dir / "metadata" / "video.json").write_text(
                json.dumps([self._video_metadata(path="video/missing.mov")])
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
                json.dumps([self._video_metadata(path="video/notes.txt")])
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
                json.dumps([self._video_metadata(path="preview/scan.mp4")])
            )

            with self.assertRaises(ScanValidationError):
                validate_scan_package(scan_dir)

    def test_validate_scan_package_rejects_frame_reference_outside_images_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            frame_path = scan_dir / "metadata" / "frames.json"
            frames = json.loads(frame_path.read_text())
            frames[0]["image"] = "metadata/session.json"
            frame_path.write_text(json.dumps(frames))

            with self.assertRaisesRegex(ScanValidationError, "inside the images directory"):
                validate_scan_package(scan_dir)

    def test_validate_scan_package_rejects_symlinked_owned_directories(self) -> None:
        for directory_name in ("images", "metadata", "video"):
            with self.subTest(directory=directory_name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                scan_dir = self._write_scan(root)
                owned_path = scan_dir / directory_name
                external_path = root / f"external_{directory_name}"

                if directory_name == "video":
                    external_path.mkdir()
                    (external_path / "legacy.mov").write_bytes(b"fake mov")
                else:
                    owned_path.rename(external_path)

                owned_path.symlink_to(external_path, target_is_directory=True)

                with self.assertRaisesRegex(ScanValidationError, "must not be a symbolic link"):
                    if directory_name == "metadata":
                        validate_and_report_scan(scan_dir)
                    else:
                        validate_scan_package(scan_dir)

                if directory_name == "metadata":
                    self.assertFalse((external_path / "manifest.json").exists())
                    self.assertFalse((external_path / "scan_report.json").exists())

    def test_validate_scan_package_rejects_symlinked_metadata_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scan_dir = self._write_scan(root)
            frames_path = scan_dir / "metadata" / "frames.json"
            external_frames = root / "external_frames.json"
            frames_path.rename(external_frames)
            frames_path.symlink_to(external_frames)

            with self.assertRaisesRegex(ScanValidationError, "symbolic links: frames.json"):
                validate_scan_package(scan_dir)

    def test_validate_scan_package_rejects_symlinked_metadata_read_write_targets(self) -> None:
        for name in (
            "manifest.json",
            "processing.json",
            "imu.json",
            "scan_report.json",
            "colmap_openmvs_plan.json",
            "mast3r_slam_neural_plan.json",
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                scan_dir = self._write_scan(root)
                external_target = root / f"external_{name}"
                external_target.write_text("external sentinel")
                (scan_dir / "metadata" / name).symlink_to(external_target)

                with self.assertRaisesRegex(ScanValidationError, f"symbolic links: {name}"):
                    validate_and_report_scan(scan_dir)

                self.assertEqual(external_target.read_text(), "external sentinel")

    def test_validate_scan_package_rejects_symlinked_optional_capture_files(self) -> None:
        for directory_name in ("depth", "arkit", "preview"):
            with self.subTest(directory=directory_name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                scan_dir = self._write_scan(root)
                capture_dir = scan_dir / directory_name
                capture_dir.mkdir()
                external_file = root / f"external_{directory_name}.bin"
                external_file.write_bytes(b"external sentinel")
                (capture_dir / "frame.bin").symlink_to(external_file)

                with self.assertRaisesRegex(ScanValidationError, "symbolic links: frame.bin"):
                    validate_and_report_scan(scan_dir)

                self.assertEqual(external_file.read_bytes(), b"external sentinel")

    def test_validate_scan_package_rejects_nested_image_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            nested_dir = scan_dir / "images" / "nested"
            nested_dir.mkdir()
            image_path = scan_dir / "images" / "frame_000001.jpg"
            image_path.rename(nested_dir / image_path.name)
            frame_path = scan_dir / "metadata" / "frames.json"
            frames = json.loads(frame_path.read_text())
            frames[0]["image"] = "images/nested/frame_000001.jpg"
            frame_path.write_text(json.dumps(frames))

            with self.assertRaisesRegex(ScanValidationError, "must not contain nested directories"):
                validate_scan_package(scan_dir)

    def test_validate_scan_package_rejects_duplicate_frame_ids_and_image_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            image_path = scan_dir / "images" / "frame_000002.jpg"
            image_path.write_bytes(b"not a real jpeg")
            frame_path = scan_dir / "metadata" / "frames.json"
            frames = json.loads(frame_path.read_text())
            duplicate = dict(frames[0])
            duplicate["image"] = "images/frame_000002.jpg"
            frames.append(duplicate)
            frame_path.write_text(json.dumps(frames))

            with self.assertRaisesRegex(ScanValidationError, "Duplicate frame id"):
                validate_scan_package(scan_dir)

            frames[1]["id"] = 2
            frames[1]["image"] = frames[0]["image"]
            frame_path.write_text(json.dumps(frames))

            with self.assertRaisesRegex(ScanValidationError, "Duplicate image reference"):
                validate_scan_package(scan_dir)

    def test_validate_scan_package_rejects_invalid_frame_scalar_contract(self) -> None:
        invalid_values = {
            "id": True,
            "timestamp": float("inf"),
            "resolution": [1920, 0],
            "blur_score": float("nan"),
        }
        for key, value in invalid_values.items():
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                scan_dir = self._write_scan(Path(tmp))
                frame_path = scan_dir / "metadata" / "frames.json"
                frames = json.loads(frame_path.read_text())
                frames[0][key] = value
                frame_path.write_text(json.dumps(frames))

                with self.assertRaises(ScanValidationError):
                    validate_scan_package(scan_dir)

    def test_validate_scan_package_rejects_non_increasing_frame_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            (scan_dir / "images" / "frame_000002.jpg").write_bytes(b"not a real jpeg")
            frame_path = scan_dir / "metadata" / "frames.json"
            frames = json.loads(frame_path.read_text())
            second = dict(frames[0])
            second.update({"id": 2, "image": "images/frame_000002.jpg"})
            frames.append(second)
            frame_path.write_text(json.dumps(frames))

            with self.assertRaisesRegex(ScanValidationError, "timestamps must increase"):
                validate_scan_package(scan_dir)

    def test_validate_scan_package_rejects_session_file_count_mismatch(self) -> None:
        count_fields = {"image_count": 2, "video_count": 1}
        for key, value in count_fields.items():
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                scan_dir = self._write_scan(Path(tmp))
                session_path = scan_dir / "metadata" / "session.json"
                session = json.loads(session_path.read_text())
                session[key] = value
                session_path.write_text(json.dumps(session))

                with self.assertRaisesRegex(ScanValidationError, f"{key} .* does not match"):
                    validate_scan_package(scan_dir)

    def test_validate_scan_package_rejects_invalid_video_scalar_contract(self) -> None:
        invalid_values = {
            "captured_at": "2026-07-07T00:00:00",
            "duration_seconds": 0,
            "frame_rate": float("nan"),
            "resolution": [1920, -1],
            "codec": "",
            "includes_audio": 0,
        }
        for key, value in invalid_values.items():
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                scan_dir = self._write_scan(Path(tmp))
                video_dir = scan_dir / "video"
                video_dir.mkdir()
                (video_dir / "scan.mov").write_bytes(b"fake mov")
                metadata = self._video_metadata()
                metadata[key] = value
                (scan_dir / "metadata" / "video.json").write_text(json.dumps([metadata]))

                with self.assertRaises(ScanValidationError):
                    validate_scan_package(scan_dir)

    def test_validate_scan_package_rejects_duplicate_or_unreferenced_video_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            video_dir = scan_dir / "video"
            video_dir.mkdir()
            (video_dir / "scan.mov").write_bytes(b"fake mov")
            video_path = scan_dir / "metadata" / "video.json"
            video_path.write_text(json.dumps([self._video_metadata(), self._video_metadata()]))

            with self.assertRaisesRegex(ScanValidationError, "Duplicate video reference"):
                validate_scan_package(scan_dir)

            video_path.write_text("[]")
            with self.assertRaisesRegex(ScanValidationError, "without metadata references"):
                validate_scan_package(scan_dir)

    def test_validate_scan_package_reports_legacy_video_without_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            video_dir = scan_dir / "video"
            video_dir.mkdir()
            (video_dir / "legacy.mov").write_bytes(b"fake mov")

            validation = validate_scan_package(scan_dir)
            report_path = write_scan_report(scan_dir, validation)
            payload = json.loads(report_path.read_text())

        self.assertEqual(validation.video_count, 1)
        self.assertEqual(validation.video_metadata_count, 0)
        self.assertEqual(validation.integrity_warnings, ("video_metadata_missing",))
        self.assertEqual(
            payload["package_integrity"]["validated_image_references"],
            1,
        )
        self.assertEqual(payload["package_integrity"]["warnings"], ["video_metadata_missing"])
        self.assertIn("video_metadata_missing", payload["warnings"])

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

    def test_blender_asset_parser_normalizes_units(self) -> None:
        module = load_blender_script_module()

        options = module.parse_blender_args(["scan.glb", "scan.blend", "--set-units", "imperial"])

        self.assertEqual(options.set_units, "IMPERIAL")

    def test_blender_import_asset_uses_legacy_obj_fallback(self) -> None:
        module = load_blender_script_module()
        calls: list[tuple[str, str]] = []
        scene = SimpleNamespace(objects=[])

        bpy = SimpleNamespace(
            context=SimpleNamespace(scene=scene),
            ops=SimpleNamespace(
                wm=SimpleNamespace(),
                import_scene=SimpleNamespace(
                    obj=lambda filepath: calls.append(("legacy_obj", filepath))
                ),
                import_mesh=SimpleNamespace(),
            ),
        )

        module.import_asset(bpy, Path("scan.obj"))

        self.assertEqual(calls, [("legacy_obj", "scan.obj")])

    def test_blender_import_asset_uses_legacy_ply_fallback(self) -> None:
        module = load_blender_script_module()
        calls: list[tuple[str, str]] = []
        scene = SimpleNamespace(objects=[])

        bpy = SimpleNamespace(
            context=SimpleNamespace(scene=scene),
            ops=SimpleNamespace(
                wm=SimpleNamespace(),
                import_scene=SimpleNamespace(),
                import_mesh=SimpleNamespace(
                    ply=lambda filepath: calls.append(("legacy_ply", filepath))
                ),
            ),
        )

        module.import_asset(bpy, Path("scan.ply"))

        self.assertEqual(calls, [("legacy_ply", "scan.ply")])

    def test_blender_relink_textures_updates_matching_images(self) -> None:
        module = load_blender_script_module()

        class FakeImage:
            def __init__(self) -> None:
                self.filepath = "missing/texture_0.jpg"

            def filepath_from_user(self) -> str:
                return self.filepath

        with tempfile.TemporaryDirectory() as tmp:
            texture_dir = Path(tmp)
            replacement = texture_dir / "texture_0.jpg"
            replacement.write_bytes(b"fake texture")
            image = FakeImage()
            bpy = SimpleNamespace(data=SimpleNamespace(images=[image]))

            module.relink_textures(bpy, texture_dir)

        self.assertEqual(image.filepath, str(replacement))

    def test_blender_prepare_asset_runs_core_operations_with_fake_bpy(self) -> None:
        module = load_blender_script_module()
        calls: list[tuple[str, object]] = []

        class FakeModifier:
            name = "scanner_decimate"

            def __init__(self) -> None:
                self.ratio = None

        class FakeModifiers:
            def new(self, name: str, type: str) -> FakeModifier:
                calls.append(("modifier_new", (name, type)))
                return FakeModifier()

        class FakeObject:
            type = "MESH"

            def __init__(self) -> None:
                self.name = "Imported"
                self.scale = (1.0, 1.0, 1.0)
                self.modifiers = FakeModifiers()

            def select_set(self, selected: bool) -> None:
                calls.append(("select_set", selected))

        fake_object = FakeObject()
        objects: list[FakeObject] = []

        class FakeWM:
            def obj_import(self, filepath: str) -> None:
                calls.append(("obj_import", filepath))
                objects.append(fake_object)

            def save_as_mainfile(self, filepath: str) -> None:
                calls.append(("save", filepath))

        bpy = SimpleNamespace(
            context=SimpleNamespace(
                scene=SimpleNamespace(objects=objects, unit_settings=SimpleNamespace(system="NONE")),
                view_layer=SimpleNamespace(objects=SimpleNamespace(active=None)),
            ),
            data=SimpleNamespace(images=[]),
            ops=SimpleNamespace(
                object=SimpleNamespace(
                    select_all=lambda action: calls.append(("select_all", action)),
                    delete=lambda: calls.append(("delete", None)),
                    transform_apply=lambda location, rotation, scale: calls.append(
                        ("transform_apply", (location, rotation, scale))
                    ),
                    origin_set=lambda type, center=None: calls.append(("origin_set", (type, center))),
                    modifier_apply=lambda modifier: calls.append(("modifier_apply", modifier)),
                ),
                wm=FakeWM(),
                import_scene=SimpleNamespace(gltf=lambda filepath: calls.append(("gltf_import", filepath))),
                export_scene=SimpleNamespace(
                    gltf=lambda filepath, export_format: calls.append(("gltf_export", (filepath, export_format)))
                ),
            ),
        )

        options = module.BlenderAssetOptions(
            input_path=Path("scan.obj"),
            output_path=Path("/tmp/scan.blend"),
            scale=2.0,
            decimate_ratio=0.5,
            export_glb=Path("/tmp/scan.glb"),
        )

        module.clear_scene(bpy)
        imported = module.import_asset(bpy, options.input_path)
        module.configure_units(bpy, options.set_units)
        module.apply_scale(bpy, imported, options.scale)
        module.set_origins(bpy, imported, options.origin)
        module.apply_decimation(bpy, imported, options.decimate_ratio)
        bpy.ops.wm.save_as_mainfile(filepath=str(options.output_path))
        bpy.ops.export_scene.gltf(filepath=str(options.export_glb), export_format="GLB")

        self.assertIn(("obj_import", "scan.obj"), calls)
        self.assertIn(("transform_apply", (False, False, True)), calls)
        self.assertIn(("origin_set", ("ORIGIN_GEOMETRY", "BOUNDS")), calls)
        self.assertIn(("modifier_apply", "scanner_decimate"), calls)
        self.assertIn(("save", "/tmp/scan.blend"), calls)
        self.assertIn(("gltf_export", ("/tmp/scan.glb", "GLB")), calls)
        self.assertEqual(bpy.context.scene.unit_settings.system, "METRIC")

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

    def test_openmvs_pipeline_runs_commands_from_dense_workspace(self) -> None:
        scan_dir = Path("/tmp/scanner-openmvs-workspace").resolve()

        with (
            patch("app.openmvs_runner.run_command") as run_command_mock,
            patch("app.openmvs_runner.inspect_openmvs_dense_cloud") as inspect_mock,
        ):
            result = run_openmvs_pipeline(scan_dir)

        self.assertEqual(result, scan_dir / "dense" / "scene_textured.obj")
        self.assertEqual(run_command_mock.call_count, 4)
        for call in run_command_mock.call_args_list:
            self.assertEqual(call.kwargs["cwd"], scan_dir / "dense")
        inspect_mock.assert_called_once()

    def test_openmvs_commands_densify_with_explicit_scope_controls(self) -> None:
        scan_dir = Path("/tmp/scanner-openmvs-scope")
        commands = build_openmvs_commands(scan_dir)

        self.assertEqual([command[0] for command in commands], [
            "InterfaceCOLMAP",
            "DensifyPointCloud",
            "ReconstructMesh",
            "TextureMesh",
        ])
        densify = commands[1]
        self.assertIn(str(scan_dir / "dense" / "scene_dense.mvs"), densify)
        self.assertEqual(densify[densify.index("--number-views-fuse") + 1], "3")
        self.assertEqual(densify[densify.index("--filter-point-cloud") + 1], "1")
        self.assertEqual(densify[densify.index("--estimate-roi") + 1], "1.1")
        self.assertEqual(densify[densify.index("--crop-to-roi") + 1], "1")
        self.assertEqual(densify[densify.index("--roi-border") + 1], "10.0")
        self.assertNotIn("-p", commands[2])

    def test_openmvs_unbounded_mode_disables_roi_crop(self) -> None:
        commands = build_openmvs_commands(
            Path("/tmp/scanner-openmvs-unbounded"),
            OpenMVSConfig(scope_mode="unbounded"),
        )
        densify = commands[1]

        self.assertEqual(densify[densify.index("--estimate-roi") + 1], "0")
        self.assertEqual(densify[densify.index("--crop-to-roi") + 1], "0")
        self.assertEqual(densify[densify.index("--roi-border") + 1], "0")

    def test_openmvs_reviewed_region_preserves_unscoped_cloud_and_crops_before_mesh(self) -> None:
        scan_dir = Path("/tmp/scanner-openmvs-reviewed")
        roi = scan_dir / "metadata" / "openmvs_region.roi"
        commands = build_openmvs_commands(scan_dir, OpenMVSConfig(region_path=roi))

        self.assertEqual([command[0] for command in commands], [
            "InterfaceCOLMAP", "DensifyPointCloud", "DensifyPointCloud",
            "ReconstructMesh", "TextureMesh",
        ])
        self.assertIn(str(scan_dir / "dense" / "scene_dense_unscoped.mvs"), commands[1])
        self.assertEqual(commands[1][commands[1].index("--crop-to-roi") + 1], "0")
        self.assertEqual(commands[2][commands[2].index("--crop-roi-file") + 1], str(roi.resolve()))
        self.assertEqual(commands[3][commands[3].index("--integrate-only-roi") + 1], "1")
        self.assertEqual(commands[3][commands[3].index("--crop-to-roi") + 1], "1")

    def test_openmvs_mask_path_and_black_ignore_label_are_forwarded_to_densification(self) -> None:
        mask_path = Path("/tmp/scanner masks")
        commands = build_openmvs_commands(
            Path("/tmp/scanner-openmvs-masks"),
            OpenMVSConfig(mask_path=mask_path),
        )

        densify = commands[1]
        self.assertEqual(densify[densify.index("--mask-path") + 1], str(mask_path.resolve()))
        self.assertEqual(densify[densify.index("--ignore-mask-label") + 1], "0")

    def test_openmvs_config_rejects_invalid_scope_values(self) -> None:
        with self.assertRaises(ValueError):
            OpenMVSConfig(number_views_fuse=0)
        with self.assertRaises(ValueError):
            OpenMVSConfig(mask_ignore_label=256)

    def test_openmvs_config_reports_effective_scope_settings(self) -> None:
        settings = OpenMVSConfig(scope_mode="unbounded", roi_border=25).report_settings()

        self.assertEqual(settings["estimate_roi"], 0)
        self.assertFalse(settings["crop_to_roi"])
        self.assertEqual(settings["roi_border"], 0)
        self.assertIsNone(settings["mask_path"])
        self.assertIsNone(settings["mask_ignore_label"])

    def test_openmvs_config_accepts_api_scope_modes(self) -> None:
        self.assertEqual(OpenMVSConfig(scope_mode="auto_roi").scope_mode, "auto_roi")
        self.assertEqual(OpenMVSConfig(scope_mode="unbounded").scope_mode, "unbounded")

        with self.assertRaises(ValueError):
            OpenMVSConfig(scope_mode="invalid")  # type: ignore[arg-type]

    def test_ply_density_budget_reads_header_without_point_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dense.ply"
            path.write_bytes(
                b"ply\nformat binary_little_endian 1.0\nelement vertex 2500001\n"
                b"property float x\nend_header\nnot-a-complete-point-payload"
            )
            result = inspect_ply_point_budget(path)

        self.assertEqual(result.point_count, 2_500_001)
        self.assertTrue(result.warning)

    def test_ply_density_budget_rejects_hard_limit_before_payload_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dense.ply"
            path.write_bytes(b"ply\nformat ascii 1.0\nelement vertex 10000001\nend_header\n")

            with self.assertRaises(PointCloudBudgetError):
                inspect_ply_point_budget(path)

    def test_ply_density_budget_rejects_invalid_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dense.ply"
            path.write_bytes(b"not-ply\n")

            with self.assertRaises(PointCloudBudgetError):
                inspect_ply_point_budget(path)

    def test_ply_density_budget_bounds_a_single_header_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dense.ply"
            path.write_bytes(b"ply\ncomment " + b"x" * (64 * 1024) + b"\nend_header\n")

            with self.assertRaises(PointCloudBudgetError):
                inspect_ply_point_budget(path)

    def test_openmvs_masks_require_complete_dimension_matched_grayscale_pngs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            images = root / "images"
            masks = root / "masks"
            images.mkdir()
            masks.mkdir()
            (images / "frame.png").write_bytes(_png_header(640, 480, color_type=2))
            (masks / "frame.mask.png").write_bytes(_grayscale_png(640, 480))

            result = validate_openmvs_masks(masks, images)

        self.assertEqual(result.image_count, 1)
        self.assertEqual(result.mask_count, 1)

    def test_openmvs_masks_reject_missing_or_mismatched_masks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            images = root / "images"
            masks = root / "masks"
            images.mkdir()
            masks.mkdir()
            (images / "frame.png").write_bytes(_png_header(640, 480, color_type=2))
            with self.assertRaises(MaskValidationError):
                validate_openmvs_masks(masks, images)

            (masks / "frame.mask.png").write_bytes(_png_header(320, 240, color_type=0))
            with self.assertRaises(MaskValidationError):
                validate_openmvs_masks(masks, images)

    def test_openmvs_masks_reject_truncated_png(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            images = root / "images"
            masks = root / "masks"
            images.mkdir()
            masks.mkdir()
            (images / "frame.png").write_bytes(_png_header(640, 480, color_type=2))
            (masks / "frame.mask.png").write_bytes(_png_header(640, 480, color_type=0)[:29])

            with self.assertRaises(MaskValidationError):
                validate_openmvs_masks(masks, images)

    def test_parse_colmap_cameras_reads_simple_radial_and_pinhole(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cameras.txt"
            path.write_text(
                "# cameras\n1 SIMPLE_RADIAL 1440 1920 1460.28 720 960 0.01359\n"
                "2 PINHOLE 1426 1901 1460.28 1460.28 713 950.5\n"
            )
            cameras = parse_colmap_cameras(path)

        self.assertEqual(cameras[1].model, "SIMPLE_RADIAL")
        self.assertEqual(cameras[2].width, 1426)

    def test_parse_colmap_cameras_rejects_non_finite_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cameras.txt"
            path.write_text("1 SIMPLE_RADIAL 10 10 nan 5 5 0\n")
            with self.assertRaises(MaskUndistortionError):
                parse_colmap_cameras(path)

    def test_parse_colmap_images_and_associate_original_dense_cameras(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "images.txt"
            path.write_text(
                "# images\n1 1 0 0 0 0 0 0 7 frame_000001.jpg\n\n"
                "2 1 0 0 0 0 0 0 7 frame_000002.jpg\n10 20 -1\n"
            )
            images = parse_colmap_image_cameras(path)
        source = {7: ColmapCamera(7, "SIMPLE_RADIAL", 5, 5, (2, 2, 2, 0))}
        target = {9: ColmapCamera(9, "PINHOLE", 3, 3, (2, 2, 1, 1))}
        associations = associate_mask_cameras(source, images, target, {name: 9 for name in images})

        self.assertEqual(images["frame_000001.jpg"], 7)
        self.assertIsInstance(associations["frame_000002.jpg"], MaskCameraAssociation)
        self.assertEqual(associations["frame_000002.jpg"].target.camera_id, 9)

    def test_associate_mask_cameras_rejects_name_or_camera_mismatch(self) -> None:
        camera = ColmapCamera(1, "SIMPLE_RADIAL", 5, 5, (2, 2, 2, 0))
        with self.assertRaises(MaskUndistortionError):
            associate_mask_cameras({1: camera}, {"a.jpg": 1}, {1: camera}, {"b.jpg": 1})
        with self.assertRaises(MaskUndistortionError):
            associate_mask_cameras({1: camera}, {"a.jpg": 2}, {1: camera}, {"a.jpg": 1})

    def test_parse_colmap_images_rejects_missing_points_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "images.txt"
            path.write_text(
                "1 1 0 0 0 0 0 0 7 first.jpg\n"
                "2 1 0 0 0 0 0 0 7 second.jpg\n\n"
            )
            with self.assertRaises(MaskUndistortionError):
                parse_colmap_image_cameras(path)

    def test_undistort_mask_array_uses_nearest_binary_sampling(self) -> None:
        source = ColmapCamera(1, "SIMPLE_RADIAL", 5, 5, (2.0, 2.0, 2.0, 0.0))
        target = ColmapCamera(1, "PINHOLE", 3, 3, (2.0, 2.0, 1.0, 1.0))
        mask = np.zeros((5, 5), dtype=np.uint8)
        mask[1:4, 1:4] = 200

        output = undistort_mask_array(mask, source, target)

        self.assertEqual(output.dtype, np.uint8)
        self.assertTrue(np.array_equal(output, np.full((3, 3), 255, dtype=np.uint8)))

    def test_undistort_mask_array_rejects_wrong_shape_or_model(self) -> None:
        source = ColmapCamera(1, "OPENCV", 5, 5, (2.0, 2.0, 2.0, 2.0))
        target = ColmapCamera(1, "PINHOLE", 3, 3, (2.0, 2.0, 1.0, 1.0))
        with self.assertRaises(MaskUndistortionError):
            undistort_mask_array(np.zeros((5, 5), dtype=np.uint8), source, target)

    def test_undistort_mask_file_writes_lossless_binary_png_without_overwrite(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed in this test environment")
        source = ColmapCamera(1, "SIMPLE_RADIAL", 5, 5, (2.0, 2.0, 2.0, 0.0))
        target = ColmapCamera(1, "PINHOLE", 3, 3, (2.0, 2.0, 1.0, 1.0))
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "capture.png"
            output_path = Path(tmp) / "dense" / "capture.mask.png"
            Image.fromarray(np.full((5, 5), 255, dtype=np.uint8)).save(input_path)

            result = undistort_mask_file(input_path, output_path, source, target)
            with Image.open(result) as written:
                values = np.asarray(written)
            with self.assertRaises(MaskUndistortionError):
                undistort_mask_file(input_path, output_path, source, target)

        self.assertTrue(np.array_equal(values, np.full((3, 3), 255, dtype=np.uint8)))

    def test_convert_capture_mask_set_stages_validates_and_publishes(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed in this test environment")
        with tempfile.TemporaryDirectory() as tmp:
            scan = Path(tmp).resolve() / "scan"
            (scan / "sparse" / "0").mkdir(parents=True)
            (scan / "dense" / "sparse").mkdir(parents=True)
            (scan / "dense" / "images").mkdir()
            (scan / "masks" / "capture").mkdir(parents=True)
            Image.fromarray(np.full((5, 5), 255, dtype=np.uint8)).save(
                scan / "masks" / "capture" / "frame.jpg.png"
            )
            (scan / "dense" / "images" / "frame.jpg").write_bytes(_png_header(3, 3, color_type=2))

            def export_model(source: Path, output: Path) -> None:
                if source == scan / "sparse" / "0":
                    camera = "1 SIMPLE_RADIAL 5 5 2 2 2 0\n"
                else:
                    camera = "1 PINHOLE 3 3 2 2 1 1\n"
                (output / "cameras.txt").write_text(camera)
                (output / "images.txt").write_text("1 1 0 0 0 0 0 0 1 frame.jpg\n\n")

            result = convert_capture_mask_set(scan, model_exporter=export_model)
            with Image.open(scan / "dense" / "masks" / "frame.mask.png") as image:
                output = np.asarray(image)

        self.assertEqual(result.mask_count, 1)
        self.assertTrue(np.array_equal(output, np.full((3, 3), 255, dtype=np.uint8)))

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

    def test_store_upload_atomically_uses_bounded_reads_and_replaces_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            destination = tmp_path / "scan.zip"
            source = RecordingAsyncReader(b"0123456789")

            byte_count = asyncio.run(
                store_upload_atomically(source, destination, chunk_size=3)
            )

            self.assertEqual(byte_count, 10)
            self.assertEqual(destination.read_bytes(), b"0123456789")
            self.assertEqual(source.read_sizes, [3, 3, 3, 3, 3])
            self.assertEqual(list(tmp_path.glob(".scan.zip.*.part")), [])

    def test_store_upload_atomically_removes_partial_file_on_read_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            destination = tmp_path / "scan.zip"
            source = RecordingAsyncReader(b"replacement", fail_on_call=2)

            with self.assertRaisesRegex(OSError, "simulated upload read failure"):
                asyncio.run(store_upload_atomically(source, destination, chunk_size=4))

            self.assertFalse(destination.exists())
            self.assertEqual(list(tmp_path.glob(".scan.zip.*.part")), [])

    def test_store_upload_atomically_cleans_up_after_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            destination = tmp_path / "scan.zip"
            source = RecordingAsyncReader(
                b"interrupted upload",
                fail_on_call=2,
                failure=asyncio.CancelledError(),
            )

            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(store_upload_atomically(source, destination, chunk_size=4))

            self.assertFalse(destination.exists())
            self.assertEqual(list(tmp_path.glob(".scan.zip.*.part")), [])

    def test_store_upload_atomically_rejects_existing_destination_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            external = tmp_path / "external.zip"
            external.write_bytes(b"external sentinel")
            destination = tmp_path / "scan.zip"
            destination.symlink_to(external)
            source = RecordingAsyncReader(b"new upload")

            with self.assertRaises(FileExistsError):
                asyncio.run(store_upload_atomically(source, destination, chunk_size=4))

            self.assertTrue(destination.is_symlink())
            self.assertEqual(external.read_bytes(), b"external sentinel")

    def test_store_upload_atomically_does_not_clobber_late_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "scan.zip"

            async def exercise() -> None:
                started = asyncio.Event()
                release = asyncio.Event()
                source = PausingAsyncReader(
                    b"upload",
                    started=started,
                    release=release,
                )
                task = asyncio.create_task(store_upload_atomically(source, destination))
                await started.wait()
                destination.write_bytes(b"late sentinel")
                release.set()
                with self.assertRaises(FileExistsError):
                    await task

            asyncio.run(exercise())

            self.assertEqual(destination.read_bytes(), b"late sentinel")
            self.assertEqual(list(Path(tmp).glob(".scan.zip.*.part")), [])

    def test_store_upload_atomically_allows_only_one_concurrent_publisher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "scan.zip"

            async def exercise() -> list[object]:
                release = asyncio.Event()
                first_started = asyncio.Event()
                second_started = asyncio.Event()
                first = PausingAsyncReader(
                    b"first",
                    started=first_started,
                    release=release,
                )
                second = PausingAsyncReader(
                    b"second",
                    started=second_started,
                    release=release,
                )
                tasks = [
                    asyncio.create_task(store_upload_atomically(first, destination)),
                    asyncio.create_task(store_upload_atomically(second, destination)),
                ]
                await first_started.wait()
                await second_started.wait()
                release.set()
                return await asyncio.gather(*tasks, return_exceptions=True)

            results = asyncio.run(exercise())

            successes = [result for result in results if isinstance(result, int)]
            collisions = [result for result in results if isinstance(result, FileExistsError)]
            published = destination.read_bytes()
            self.assertEqual(successes, [len(published)])
            self.assertEqual(len(collisions), 1)
            self.assertIn(published, {b"first", b"second"})
            self.assertEqual(list(Path(tmp).glob(".scan.zip.*.part")), [])

    def test_store_upload_cancellation_does_not_delete_foreign_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            destination = tmp_path / "scan.zip"
            source = RecordingAsyncReader(b"upload")
            directory_sync_started = threading.Event()
            release_directory_sync = threading.Event()
            sync_calls = 0

            def block_first_directory_sync(_: Path) -> None:
                nonlocal sync_calls
                sync_calls += 1
                if sync_calls == 1:
                    directory_sync_started.set()
                    release_directory_sync.wait(timeout=2)

            async def exercise() -> None:
                with patch(
                    "app.storage._sync_directory",
                    side_effect=block_first_directory_sync,
                ):
                    task = asyncio.create_task(store_upload_atomically(source, destination))
                    while not directory_sync_started.is_set():
                        await asyncio.sleep(0.001)
                    replacement = tmp_path / "foreign.zip"
                    replacement.write_bytes(b"foreign replacement")
                    os.replace(replacement, destination)
                    task.cancel()
                    release_directory_sync.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await task

            asyncio.run(exercise())

            self.assertEqual(destination.read_bytes(), b"foreign replacement")
            self.assertEqual(list(tmp_path.glob(".scan.zip.*.part")), [])

    def test_store_upload_atomically_keeps_event_loop_responsive_during_slow_fsync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "scan.zip"
            source = RecordingAsyncReader(b"upload")
            fsync_started = threading.Event()
            original_fsync = os.fsync

            def slow_fsync(descriptor: int) -> None:
                fsync_started.set()
                time.sleep(0.25)
                original_fsync(descriptor)

            async def exercise() -> float:
                with patch("app.storage.os.fsync", side_effect=slow_fsync):
                    task = asyncio.create_task(store_upload_atomically(source, destination))
                    while not fsync_started.is_set():
                        await asyncio.sleep(0.001)
                    started_at = time.perf_counter()
                    await asyncio.sleep(0.02)
                    responsiveness = time.perf_counter() - started_at
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    return responsiveness

            responsiveness = asyncio.run(exercise())

            self.assertLess(responsiveness, 0.1)
            self.assertFalse(destination.exists())
            self.assertEqual(list(Path(tmp).glob(".scan.zip.*.part")), [])

    def test_store_upload_atomically_orders_sync_link_publication_and_temp_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "scan.zip"
            source = RecordingAsyncReader(b"upload")
            events: list[str] = []
            original_link = os.link

            def sync_and_close(file) -> None:
                events.append("file_sync")
                file.flush()
                file.close()

            def link(source_path: Path, destination_path: Path) -> None:
                events.append("link")
                original_link(source_path, destination_path)

            def sync_directory(_: Path) -> None:
                events.append("directory_sync")

            def remove_temporary(temporary_path: Path) -> None:
                events.append("remove_temporary")
                temporary_path.unlink()
                sync_directory(temporary_path.parent)

            with (
                patch("app.storage._sync_and_close", side_effect=sync_and_close),
                patch("app.storage.os.link", side_effect=link),
                patch("app.storage._sync_directory", side_effect=sync_directory),
                patch("app.storage._remove_temporary_name", side_effect=remove_temporary),
            ):
                asyncio.run(store_upload_atomically(source, destination))

            self.assertEqual(
                events,
                [
                    "file_sync",
                    "link",
                    "directory_sync",
                    "remove_temporary",
                    "directory_sync",
                ],
            )
            self.assertEqual(destination.read_bytes(), b"upload")

    def test_store_upload_atomically_rolls_back_cancellation_during_directory_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            destination = tmp_path / "scan.zip"
            source = RecordingAsyncReader(b"upload")
            directory_sync_started = threading.Event()
            sync_calls = 0

            def slow_first_directory_sync(_: Path) -> None:
                nonlocal sync_calls
                sync_calls += 1
                if sync_calls == 1:
                    directory_sync_started.set()
                    time.sleep(0.2)

            async def exercise() -> None:
                with patch(
                    "app.storage._sync_directory",
                    side_effect=slow_first_directory_sync,
                ):
                    task = asyncio.create_task(store_upload_atomically(source, destination))
                    while not directory_sync_started.is_set():
                        await asyncio.sleep(0.001)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task

            asyncio.run(exercise())

            self.assertGreaterEqual(sync_calls, 2)
            self.assertFalse(destination.exists())
            self.assertEqual(list(tmp_path.glob(".scan.zip.*.part")), [])

    def test_store_upload_atomically_removes_published_file_after_directory_sync_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            destination = tmp_path / "scan.zip"
            source = RecordingAsyncReader(b"upload")

            with (
                patch("app.storage._sync_directory", side_effect=OSError("directory sync failed")),
                self.assertRaisesRegex(OSError, "directory sync failed"),
            ):
                asyncio.run(store_upload_atomically(source, destination))

            self.assertFalse(destination.exists())
            self.assertEqual(list(tmp_path.glob(".scan.zip.*.part")), [])

    def test_store_job_upload_preserves_primary_errors_when_job_failure_recording_fails(self) -> None:
        scenarios = (
            (
                OSError("upload read failure"),
                OSError,
                "upload read failure",
            ),
            (
                asyncio.CancelledError(),
                asyncio.CancelledError,
                None,
            ),
        )
        for primary_error, expected_type, message in scenarios:
            with self.subTest(error=expected_type.__name__), tempfile.TemporaryDirectory() as tmp:
                source = RecordingAsyncReader(
                    b"upload",
                    fail_on_call=1,
                    failure=primary_error,
                )
                jobs = RecordingUploadJobStore(failure=OSError("job write failure"))

                with self.assertRaises(expected_type) as raised:
                    asyncio.run(
                        store_job_upload(
                            source,
                            Path(tmp) / "scan.zip",
                            scan_id="scan-1",
                            jobs=jobs,
                        )
                    )

                if message is not None:
                    self.assertIn(message, str(raised.exception))
                self.assertEqual(len(jobs.updates), 1)

    def test_store_upload_atomically_rejects_invalid_chunk_size(self) -> None:
        for chunk_size in (0, -1, True):
            with self.subTest(chunk_size=chunk_size), tempfile.TemporaryDirectory() as tmp:
                destination = Path(tmp) / "scan.zip"
                source = RecordingAsyncReader(b"upload")

                with self.assertRaisesRegex(ValueError, "positive integer"):
                    asyncio.run(
                        store_upload_atomically(
                            source,
                            destination,
                            chunk_size=chunk_size,
                        )
                    )

                self.assertFalse(destination.exists())

    def test_job_store_lists_recent_jobs_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            timestamps = iter(
                [
                    datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc),
                    datetime(2026, 7, 9, 12, 1, tzinfo=timezone.utc),
                    datetime(2026, 7, 9, 12, 2, tzinfo=timezone.utc),
                    datetime(2026, 7, 9, 12, 3, tzinfo=timezone.utc),
                ]
            )
            store = JobStore(Path(tmp), clock=lambda: next(timestamps))
            store.create("old")
            store.create("new")
            store.update("old", status="processing", stage="validating", message="older")
            store.update("new", status="processing", stage="validating", message="newer")

            jobs = store.list()

        self.assertEqual([job.scan_id for job in jobs], ["new", "old"])

    def test_job_store_list_respects_limit_and_skips_invalid_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            store.create("valid")
            valid_path = Path(tmp) / "valid.json"
            broken_path = Path(tmp) / "broken.json"
            broken_path.write_text("{")
            os.utime(valid_path, (1000, 1000))
            os.utime(broken_path, (2000, 2000))

            jobs = store.list(limit=1)

        self.assertEqual([job.scan_id for job in jobs], ["valid"])

    def test_job_store_records_lifecycle_timestamps_and_stages(self) -> None:
        start = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)
        timestamps = iter(
            [
                start,
                start + timedelta(seconds=2),
                start + timedelta(seconds=4),
                start + timedelta(seconds=6),
                start + timedelta(seconds=8),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp), clock=lambda: next(timestamps))
            received = store.create("scan-1")
            processing = store.update(
                "scan-1",
                status="processing",
                stage="validating",
                message="Validating scan package.",
            )
            store.update("scan-1", status="processing", stage="reconstructing")
            store.update("scan-1", status="processing", stage="exporting")
            finished = store.update(
                "scan-1",
                status="complete",
                message="Reconstruction completed.",
            )

        self.assertEqual(received.stage, "received")
        self.assertEqual(received.created_at, "2026-07-09T12:00:00+00:00")
        self.assertEqual(received.updated_at, received.created_at)
        self.assertIsNone(received.started_at)
        self.assertIsNone(received.finished_at)
        self.assertEqual(processing.stage, "validating")
        self.assertEqual(processing.started_at, "2026-07-09T12:00:02+00:00")
        self.assertEqual(finished.stage, "finished")
        self.assertEqual(finished.started_at, processing.started_at)
        self.assertEqual(finished.finished_at, "2026-07-09T12:00:08+00:00")

    def test_job_store_rejects_restart_after_terminal_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            store.create("scan-1")
            store.update("scan-1", status="processing", stage="validating")
            store.update("scan-1", status="validated")

            with self.assertRaises(JobTransitionError):
                store.update("scan-1", status="processing", stage="validating")

    def test_job_store_rejects_backward_stage_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            store.create("scan-1")

            with self.assertRaises(JobTransitionError):
                store.update("scan-1", status="processing", stage="reconstructing")

            store.update("scan-1", status="processing", stage="validating")
            store.update("scan-1", status="processing", stage="reconstructing")
            store.update("scan-1", status="processing", stage="exporting")

            with self.assertRaises(JobTransitionError):
                store.update("scan-1", status="processing", stage="queued")

    def test_job_store_requires_terminal_status_to_follow_matching_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            store.create("scan-1")
            store.update("scan-1", status="processing", stage="validating")

            with self.assertRaises(JobTransitionError):
                store.update("scan-1", status="complete")

    def test_job_store_terminal_updates_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            store.create("validated")
            store.update("validated", status="processing", stage="validating")
            store.update("validated", status="validated")

            store.create("complete")
            store.update("complete", status="processing", stage="validating")
            store.update("complete", status="processing", stage="reconstructing")
            store.update("complete", status="processing", stage="exporting")
            store.update("complete", status="complete")

            store.create("failed")
            store.update("failed", status="failed")

            validated = store.update("validated", status="validated", message="still valid")
            complete = store.update("complete", status="complete", message="still complete")
            failed = store.update("failed", status="failed", message="still failed")

        self.assertEqual(validated.stage, "finished")
        self.assertEqual(complete.stage, "finished")
        self.assertEqual(failed.stage, "finished")

    def test_job_store_failed_write_preserves_last_valid_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            original = store.create("scan-1")

            with patch("app.jobs.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.update(
                        "scan-1",
                        status="processing",
                        stage="validating",
                        message="working",
                    )

            persisted = store.read("scan-1")
            temporary_files = list(Path(tmp).glob("*.tmp"))

        self.assertEqual(persisted.status, original.status)
        self.assertEqual(persisted.updated_at, original.updated_at)
        self.assertEqual(temporary_files, [])

    def test_job_store_requires_stage_when_processing_starts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            store.create("scan-1")

            with self.assertRaises(ValueError):
                store.update("scan-1", status="processing")

    def test_job_store_serializes_concurrent_terminal_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            store.create("scan-1")
            store.update("scan-1", status="processing", stage="validating")
            store.update("scan-1", status="processing", stage="reconstructing")
            store.update("scan-1", status="processing", stage="exporting")

            stale_write_started = threading.Event()
            release_stale_write = threading.Event()
            terminal_started = threading.Event()
            terminal_finished = threading.Event()
            errors: list[Exception] = []
            original_write = store._write

            def blocking_write(record) -> None:
                if threading.current_thread().name == "stale-update":
                    stale_write_started.set()
                    if not release_stale_write.wait(timeout=2):
                        raise TimeoutError("Timed out waiting to release stale update")
                original_write(record)

            store._write = blocking_write

            def update_processing() -> None:
                try:
                    store.update("scan-1", status="processing", stage="exporting")
                except Exception as error:
                    errors.append(error)

            def update_terminal() -> None:
                terminal_started.set()
                try:
                    store.update("scan-1", status="complete")
                except Exception as error:
                    errors.append(error)
                finally:
                    terminal_finished.set()

            stale_thread = threading.Thread(target=update_processing, name="stale-update")
            terminal_thread = threading.Thread(target=update_terminal, name="terminal-update")
            stale_thread.start()
            self.assertTrue(stale_write_started.wait(timeout=2))
            terminal_thread.start()
            self.assertTrue(terminal_started.wait(timeout=2))
            self.assertFalse(terminal_finished.wait(timeout=0.2))
            release_stale_write.set()
            stale_thread.join(timeout=2)
            terminal_thread.join(timeout=2)

            final_record = store.read("scan-1")

        self.assertEqual(errors, [])
        self.assertEqual(final_record.status, "complete")
        self.assertEqual(final_record.stage, "finished")

    def test_job_recovery_preserves_partial_and_recovers_completed_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs_dir = root / "jobs"
            processing_dir = root / "processing"
            completed_dir = root / "completed"
            failed_dir = root / "failed"
            for directory in [jobs_dir, processing_dir, completed_dir, failed_dir]:
                directory.mkdir(parents=True)

            store = JobStore(jobs_dir)
            store.create("partial")
            store.update("partial", status="processing", stage="validating")
            partial_path = processing_dir / "partial"
            partial_path.mkdir()
            (partial_path / "partial-output.txt").write_text("keep me")
            existing_failed_path = failed_dir / "partial"
            existing_failed_path.mkdir()
            (existing_failed_path / "earlier-failure.txt").write_text("keep this too")

            store.create("validated")
            store.update("validated", status="processing", stage="validating")
            self._write_scan(completed_dir / "validated")

            store.create("complete")
            store.update("complete", status="processing", stage="validating")
            store.update("complete", status="processing", stage="reconstructing")
            store.update(
                "complete",
                status="processing",
                stage="exporting",
                outputs={
                    "colmap_output": str(
                        processing_dir
                        / "complete"
                        / "scan_test"
                        / "dense"
                        / "fused.ply"
                    ),
                    "textured_mesh": str(
                        processing_dir
                        / "complete"
                        / "scan_test"
                        / "dense"
                        / "scene_textured.obj"
                    ),
                },
            )
            complete_scan = self._write_scan(completed_dir / "complete")
            complete_dense = complete_scan / "dense"
            complete_dense.mkdir()
            (complete_dense / "fused.ply").write_bytes(b"dense cloud")
            (complete_dense / "scene_textured.obj").write_bytes(b"textured mesh")
            (complete_scan / "metadata" / "scan_report.json").write_text("{}")

            for scan_id in ["missing-output", "directory-output"]:
                store.create(scan_id)
                store.update(scan_id, status="processing", stage="validating")
                store.update(scan_id, status="processing", stage="reconstructing")
                store.update(
                    scan_id,
                    status="processing",
                    stage="exporting",
                    outputs={
                        "colmap_output": str(
                            processing_dir
                            / scan_id
                            / "scan_test"
                            / "dense"
                            / "fused.ply"
                        )
                    },
                )
                incomplete_scan = self._write_scan(completed_dir / scan_id)
                (incomplete_scan / "metadata" / "scan_report.json").write_text("{}")
                if scan_id == "directory-output":
                    (incomplete_scan / "dense" / "fused.ply").mkdir(parents=True)

            reconciled = reconcile_interrupted_jobs(
                store,
                processing_dir=processing_dir,
                completed_dir=completed_dir,
                failed_dir=failed_dir,
            )

            partial = store.read("partial")
            validated = store.read("validated")
            complete = store.read("complete")
            missing_output = store.read("missing-output")
            directory_output = store.read("directory-output")

            self.assertEqual(
                {record.scan_id for record in reconciled},
                {
                    "partial",
                    "validated",
                    "complete",
                    "missing-output",
                    "directory-output",
                },
            )
            self.assertEqual(partial.status, "failed")
            self.assertTrue((existing_failed_path / "earlier-failure.txt").is_file())
            moved_partial_path = failed_dir / "partial-interrupted-1"
            self.assertTrue((moved_partial_path / "partial-output.txt").is_file())
            self.assertEqual(partial.outputs["package_dir"], str(moved_partial_path))
            self.assertEqual(validated.status, "validated")
            self.assertEqual(
                validated.outputs["package_dir"],
                str(completed_dir / "validated"),
            )
            self.assertEqual(complete.status, "complete")
            self.assertEqual(complete.image_count, 1)
            self.assertEqual(
                complete.outputs["package_dir"],
                str(completed_dir / "complete"),
            )
            self.assertEqual(
                complete.outputs["colmap_output"],
                str((complete_scan / "dense" / "fused.ply").resolve()),
            )
            self.assertEqual(
                complete.outputs["textured_mesh"],
                str((complete_scan / "dense" / "scene_textured.obj").resolve()),
            )
            self.assertEqual(
                complete.outputs["scan_report"],
                str((complete_scan / "metadata" / "scan_report.json").resolve()),
            )
            for recovered in [missing_output, directory_output]:
                self.assertEqual(recovered.status, "failed")
                self.assertIn("no safe dense or sparse COLMAP result", recovered.message)
                self.assertNotIn("colmap_output", recovered.outputs)
                self.assertTrue(Path(recovered.outputs["package_dir"]).is_dir())

    def test_job_recovery_retries_collision_after_terminal_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs_dir = root / "jobs"
            processing_dir = root / "processing"
            completed_dir = root / "completed"
            failed_dir = root / "failed"
            for directory in [jobs_dir, processing_dir, completed_dir, failed_dir]:
                directory.mkdir(parents=True)

            store = JobStore(jobs_dir)
            store.create("scan-1")
            store.update("scan-1", status="processing", stage="validating")
            processing_path = processing_dir / "scan-1"
            processing_path.mkdir()
            (processing_path / "current-partial.txt").write_text("current")
            earlier_failed_path = failed_dir / "scan-1"
            earlier_failed_path.mkdir()
            (earlier_failed_path / "earlier-partial.txt").write_text("earlier")

            original_update = store.update
            terminal_write_failed = False

            def fail_first_terminal_update(scan_id: str, **values):
                nonlocal terminal_write_failed
                if values["status"] == "failed" and not terminal_write_failed:
                    terminal_write_failed = True
                    raise OSError("injected status write failure")
                return original_update(scan_id, **values)

            store.update = fail_first_terminal_update
            with self.assertRaises(OSError):
                reconcile_interrupted_jobs(
                    store,
                    processing_dir=processing_dir,
                    completed_dir=completed_dir,
                    failed_dir=failed_dir,
                )

            interrupted = store.read("scan-1")
            interrupted_path = failed_dir / "scan-1-interrupted-1"
            self.assertEqual(interrupted.status, "processing")
            self.assertEqual(
                interrupted.outputs["interrupted_workspace"],
                str(interrupted_path),
            )
            self.assertTrue((interrupted_path / "current-partial.txt").is_file())
            self.assertTrue((earlier_failed_path / "earlier-partial.txt").is_file())

            store.update = original_update
            reconcile_interrupted_jobs(
                store,
                processing_dir=processing_dir,
                completed_dir=completed_dir,
                failed_dir=failed_dir,
            )
            recovered = store.read("scan-1")

            self.assertEqual(recovered.status, "failed")
            self.assertEqual(recovered.outputs["package_dir"], str(interrupted_path))
            self.assertNotIn("interrupted_workspace", recovered.outputs)
            self.assertTrue((interrupted_path / "current-partial.txt").is_file())

    def test_job_store_rejects_unsafe_scan_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))

            with self.assertRaises(ValueError):
                store.create("../outside")

    def test_job_store_reads_legacy_record_without_lifecycle_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jobs_dir = Path(tmp)
            (jobs_dir / "legacy.json").write_text(
                json.dumps({"scan_id": "legacy", "status": "validated"})
            )
            store = JobStore(jobs_dir)

            record = store.read("legacy")

        self.assertEqual(record.status, "validated")
        self.assertIsNone(record.stage)
        self.assertIsNone(record.created_at)
        self.assertIsNone(record.finished_at)

    def test_get_scan_status_rejects_malformed_scan_id(self) -> None:
        if importlib.util.find_spec("fastapi") is None:
            self.skipTest("FastAPI is not installed in the lightweight test environment")

        from app import main as backend_main
        from fastapi import HTTPException

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(backend_main, "jobs", JobStore(Path(tmp))):
                with self.assertRaises(HTTPException) as raised:
                    backend_main.get_scan_status("bad!")

        self.assertEqual(raised.exception.status_code, 400)

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

    def test_inspect_scan_cli_prints_integrity_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_dir = self._write_scan(Path(tmp))
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "inspect_scan.py"), str(scan_dir)],
                check=True,
                capture_output=True,
                text=True,
            )

        self.assertIn("images=1", result.stdout)
        self.assertIn("video_metadata_entries=0", result.stdout)
        self.assertIn("integrity_warnings=none", result.stdout)

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
            script = ROOT / "scripts" / "benchmark_evidence.py"

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
        self.assertEqual(plan.commands[1][0:2], ["ns-train", "splatfacto"])
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
                json.dumps([self._video_metadata(path="video/scan.mp4")])
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
                    str(ROOT / "scripts" / "plan_neural_backend.py"),
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

    def _video_metadata(self, *, path: str = "video/scan.mov") -> dict[str, object]:
        return {
            "path": path,
            "captured_at": "2026-07-07T00:00:00Z",
            "duration_seconds": 12.5,
            "frame_rate": 30,
            "resolution": [1920, 1080],
            "codec": "h264",
            "includes_audio": False,
        }

    def _zip_scan(self, scan_dir: Path, archive: Path, root: Path) -> None:
        if archive.exists():
            archive.unlink()
        with zipfile.ZipFile(archive, "w") as zip_file:
            for path in scan_dir.rglob("*"):
                if path.is_file():
                    zip_file.write(path, path.relative_to(root))


if __name__ == "__main__":
    unittest.main()
