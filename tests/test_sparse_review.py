from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.job_recovery import reconcile_interrupted_jobs  # noqa: E402
from app.jobs import JobStore  # noqa: E402
from app.sparse_review import (  # noqa: E402
    SparseReviewError,
    publish_sparse_review_checkpoint,
)


IMAGES_TEXT = """# Image list
2 1 0 0 0 1 2 3 4 nested/b.jpg
10 20 -1
1 0.7071067811865476 0 0 0.7071067811865476 1 0 0 4 a.jpg
30 40 12
"""


class SparseReviewTests(unittest.TestCase):
    def test_publishes_camera_preview_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_root = self._write_sparse_scan(Path(tmp))

            outputs = publish_sparse_review_checkpoint(
                scan_root,
                run_dense=True,
                run_openmvs=True,
                scope_mode="auto_roi",
                use_masks=True,
                model_exporter=self._write_text_model,
            )

            cameras = json.loads(outputs["sparse_camera_preview"].read_text())
            checkpoint = json.loads(outputs["scope_review_checkpoint"].read_text())

        self.assertEqual(cameras["camera_count"], 2)
        self.assertEqual([camera["image_id"] for camera in cameras["cameras"]], [1, 2])
        for actual, expected in zip(cameras["cameras"][0]["center"], [0.0, 1.0, 0.0]):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(cameras["cameras"][1]["center"], [-1.0, -2.0, -3.0])
        self.assertEqual(checkpoint["state"], "awaiting_scope")
        self.assertEqual(checkpoint["coordinate_system"], "colmap_reconstruction")
        self.assertEqual(
            checkpoint["continuation"],
            {
                "run_dense": True,
                "run_openmvs": True,
                "scope_mode": "auto_roi",
                "use_masks": True,
            },
        )
        self.assertEqual(outputs["sparse_point_cloud"].name, "sparse_points.ply")

    def test_rejects_missing_sparse_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_root = Path(tmp)
            (scan_root / "sparse" / "0").mkdir(parents=True)

            with self.assertRaisesRegex(SparseReviewError, "point-cloud preview"):
                publish_sparse_review_checkpoint(
                    scan_root,
                    run_dense=True,
                    run_openmvs=True,
                    scope_mode="auto_roi",
                    use_masks=False,
                    model_exporter=self._write_text_model,
                )

    def test_rejects_invalid_camera_quaternion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_root = self._write_sparse_scan(Path(tmp))

            def invalid_exporter(_: Path, output: Path) -> None:
                (output / "images.txt").write_text(
                    "1 2 0 0 0 0 0 0 1 frame.jpg\n10 20 -1\n"
                )

            with self.assertRaisesRegex(SparseReviewError, "quaternion"):
                publish_sparse_review_checkpoint(
                    scan_root,
                    run_dense=False,
                    run_openmvs=False,
                    scope_mode="unbounded",
                    use_masks=False,
                    model_exporter=invalid_exporter,
                )

    def test_awaiting_scope_transition_can_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            store.create("scan-1")
            store.update("scan-1", status="processing", stage="validating")
            store.update("scan-1", status="processing", stage="reconstructing")
            paused = store.update("scan-1", status="processing", stage="awaiting_scope")
            resumed = store.update("scan-1", status="processing", stage="reconstructing")

        self.assertEqual(paused.stage, "awaiting_scope")
        self.assertEqual(resumed.stage, "reconstructing")

    def test_recovery_preserves_intentional_scope_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            processing_dir = root / "processing"
            completed_dir = root / "completed"
            failed_dir = root / "failed"
            workspace = processing_dir / "scan-1"
            workspace.mkdir(parents=True)
            (workspace / "checkpoint.txt").write_text("keep")
            store = JobStore(root / "jobs")
            store.create("scan-1")
            store.update("scan-1", status="processing", stage="validating")
            store.update("scan-1", status="processing", stage="reconstructing")
            expected = store.update("scan-1", status="processing", stage="awaiting_scope")

            reconciled = reconcile_interrupted_jobs(
                store,
                processing_dir=processing_dir,
                completed_dir=completed_dir,
                failed_dir=failed_dir,
            )

            persisted_stage = store.read("scan-1").stage
            checkpoint_exists = (workspace / "checkpoint.txt").is_file()
            failed_exists = failed_dir.exists()

        self.assertEqual(reconciled, [expected])
        self.assertEqual(persisted_stage, "awaiting_scope")
        self.assertTrue(checkpoint_exists)
        self.assertFalse(failed_exists)

    def test_process_scan_pauses_after_sparse_reconstruction(self) -> None:
        if importlib.util.find_spec("fastapi") is None:
            self.skipTest("FastAPI is not installed in the lightweight test environment")

        from app import main as backend_main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "processing" / "scan-1"
            scan_root = workspace / "scan"
            sparse_points = scan_root / "sparse" / "sparse_points.ply"
            camera_preview = scan_root / "sparse" / "cameras_preview.json"
            checkpoint = scan_root / "metadata" / "reconstruction_checkpoint.json"
            store = JobStore(root / "jobs")
            store.create("scan-1")
            store.update("scan-1", status="processing", stage="queued")
            record_step = Mock()
            package = SimpleNamespace(
                validation=SimpleNamespace(
                    image_count=12,
                    frame_count=12,
                    reconstruction_scope=None,
                ),
                report_path=scan_root / "metadata" / "scan_report.json",
                record_processing_step=record_step,
            )

            with (
                patch.object(backend_main, "jobs", store),
                patch.object(backend_main, "prepare_processing_dir", return_value=workspace),
                patch.object(backend_main, "find_scan_root", return_value=scan_root),
                patch.object(backend_main, "validate_and_report_scan", return_value=package),
                patch.object(
                    backend_main,
                    "run_colmap_pipeline",
                    return_value=sparse_points,
                ) as run_colmap,
                patch.object(
                    backend_main,
                    "publish_sparse_review_checkpoint",
                    return_value={
                        "sparse_point_cloud": sparse_points,
                        "sparse_camera_preview": camera_preview,
                        "scope_review_checkpoint": checkpoint,
                    },
                ),
                patch.object(backend_main, "run_openmvs_pipeline") as run_openmvs,
                patch.object(backend_main, "export_blender_formats") as export_blender,
            ):
                backend_main.process_scan(
                    "scan-1",
                    root / "incoming.zip",
                    True,
                    True,
                    "auto_roi",
                    False,
                    True,
                )

            paused = store.read("scan-1")

        self.assertEqual(paused.status, "processing")
        self.assertEqual(paused.stage, "awaiting_scope")
        self.assertEqual(paused.image_count, 12)
        self.assertEqual(paused.outputs["package_dir"], str(workspace))
        self.assertNotIn("colmap_output", paused.outputs)
        self.assertIn("sparse_point_cloud", paused.outputs)
        self.assertIn("sparse_camera_preview", paused.outputs)
        run_colmap.assert_called_once()
        self.assertFalse(run_colmap.call_args.kwargs["include_dense"])
        run_openmvs.assert_not_called()
        export_blender.assert_not_called()
        self.assertEqual(record_step.call_args_list[-1].args[0], "scope_review_checkpoint")

    @staticmethod
    def _write_sparse_scan(root: Path) -> Path:
        (root / "sparse" / "0").mkdir(parents=True)
        (root / "metadata").mkdir()
        (root / "sparse" / "sparse_points.ply").write_bytes(
            b"ply\nformat ascii 1.0\nelement vertex 1\n"
            b"property float x\nproperty float y\nproperty float z\n"
            b"end_header\n0 0 0\n"
        )
        return root

    @staticmethod
    def _write_text_model(_: Path, output: Path) -> None:
        (output / "images.txt").write_text(IMAGES_TEXT)


if __name__ == "__main__":
    unittest.main()
