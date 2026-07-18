from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.mask_generator import generate_mask_proposals  # noqa: E402
from app.mask_review import (  # noqa: E402
    MaskReviewBlockedError,
    MaskReviewError,
    approve_mask_review,
    load_mask_review,
    reject_mask_review,
)
from app.scan_metadata import load_scan_metadata  # noqa: E402
from app.scan_validator import validate_scan_package  # noqa: E402


class MaskReviewTests(unittest.TestCase):
    def test_approval_promotes_exact_set_and_activates_manifest_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._scan(Path(tmp), count=3)
            frames = load_scan_metadata(root / "metadata").frames
            result = generate_mask_proposals(root, frames)
            assert result is not None

            approved = approve_mask_review(
                root,
                clock=lambda: datetime(2026, 7, 18, tzinfo=timezone.utc),
            )
            validation = validate_scan_package(root)
            manifest = json.loads((root / "metadata" / "manifest.json").read_text())

        self.assertEqual(approved["state"], "approved")
        self.assertEqual(approved["decision"]["promoted_mask_count"], 3)
        self.assertEqual(validation.capture_mask_count, 3)
        self.assertEqual(manifest["reconstruction_scope"]["mask_count"], 3)

    def test_quality_failure_cannot_be_approved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._scan(Path(tmp), count=2, abrupt=True)
            frames = load_scan_metadata(root / "metadata").frames
            result = generate_mask_proposals(root, frames)
            assert result is not None

            with self.assertRaises(MaskReviewBlockedError):
                approve_mask_review(root)

            self.assertFalse((root / "masks" / "capture").exists())
            self.assertIsNone(load_scan_metadata(root / "metadata").reconstruction_scope)

    def test_rejection_preserves_proposals_but_cannot_be_decided_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._scan(Path(tmp), count=2)
            frames = load_scan_metadata(root / "metadata").frames
            generate_mask_proposals(root, frames)

            rejected = reject_mask_review(root)
            report = load_mask_review(root)
            with self.assertRaises(MaskReviewError):
                reject_mask_review(root)
            proposals_exist = (root / "masks" / "proposed").is_dir()

        self.assertEqual(rejected["state"], "rejected")
        self.assertEqual(report["state"], "rejected")
        self.assertTrue(proposals_exist)

    def test_approval_refuses_replaced_proposal_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._scan(Path(tmp), count=2)
            frames = load_scan_metadata(root / "metadata").frames
            generate_mask_proposals(root, frames)
            proposal = root / "masks" / "proposed" / "frame_000000.jpg.png"
            proposal.unlink()
            proposal.symlink_to(root / "images" / "frame_000000.jpg")

            with self.assertRaises(MaskReviewError):
                approve_mask_review(root)

            capture_exists = (root / "masks" / "capture").exists()

        self.assertFalse(capture_exists)

    @staticmethod
    def _scan(root: Path, *, count: int, abrupt: bool = False) -> Path:
        scan = root / "scan"
        images = scan / "images"
        metadata = scan / "metadata"
        images.mkdir(parents=True)
        metadata.mkdir()
        frames: list[dict[str, object]] = []
        for index in range(count):
            name = f"frame_{index:06d}.jpg"
            Image.new("RGB", (100, 100), color=(80, 100, 120)).save(images / name)
            frames.append({
                "id": index,
                "image": f"images/{name}",
                "timestamp": float(index),
                "resolution": [100, 100],
            })
        (metadata / "frames.json").write_text(json.dumps(frames))
        (metadata / "session.json").write_text(json.dumps({"scan_id": "review"}))
        first_points = (
            [
                {"x": 0.45, "y": 0.45}, {"x": 0.50, "y": 0.45},
                {"x": 0.50, "y": 0.50}, {"x": 0.45, "y": 0.50},
            ]
            if abrupt else
            [
                {"x": 0.1, "y": 0.1}, {"x": 0.6, "y": 0.1},
                {"x": 0.6, "y": 0.8}, {"x": 0.1, "y": 0.8},
            ]
        )
        selections = []
        for index in (0, count - 1):
            points = first_points if index == 0 else [
                {"x": 0.2, "y": 0.1}, {"x": 0.8, "y": 0.1},
                {"x": 0.8, "y": 0.8}, {"x": 0.2, "y": 0.8},
            ]
            selections.append({
                "frame_id": index,
                "image": frames[index]["image"],
                "regions": [{"operation": "keep", "points": points}],
            })
        (metadata / "mask_authoring.json").write_text(json.dumps({
            "schema_version": "1.0",
            "authoring_mode": "representative_frames",
            "coordinate_space": "normalized_capture_image",
            "mask_convention": "white_keep_black_exclude",
            "revision": 1,
            "representative_frames": selections,
        }))
        return scan


if __name__ == "__main__":
    unittest.main()
