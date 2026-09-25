from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

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
from app.scan_validator import ScanValidationError, validate_scan_package  # noqa: E402


class MaskReviewTests(unittest.TestCase):
    def test_approval_promotes_exact_set_and_activates_manifest_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._scan(Path(tmp), count=21)
            frames = load_scan_metadata(root / "metadata").frames
            result = generate_mask_proposals(root, frames)
            assert result is not None
            self.assertEqual(len(result.review_indices), 5)
            proposal_bytes = {
                path.name: path.read_bytes() for path in (root / "masks/proposed").iterdir()
            }

            approved = approve_mask_review(
                root,
                clock=lambda: datetime(2026, 7, 18, tzinfo=timezone.utc),
            )
            validation = validate_scan_package(root)
            manifest = json.loads((root / "metadata" / "manifest.json").read_text())
            self.assertEqual(
                proposal_bytes,
                {path.name: path.read_bytes() for path in (root / "masks/capture").iterdir()},
            )

        self.assertEqual(approved["state"], "approved")
        self.assertEqual(approved["decision"]["promoted_mask_count"], 21)
        self.assertEqual(validation.capture_mask_count, 21)
        self.assertEqual(manifest["reconstruction_scope"]["mask_count"], 21)

    def test_approval_validates_unsampled_proposals_not_just_the_five_previews(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._scan(Path(tmp), count=21)
            frames = load_scan_metadata(root / "metadata").frames
            result = generate_mask_proposals(root, frames)
            assert result is not None
            index = next(index for index in range(len(frames)) if index not in result.review_indices)
            proposal = root / result.frames[index].mask
            proposal.write_bytes(b"not a PNG")

            with self.assertRaisesRegex(MaskReviewError, "promotion validation"):
                approve_mask_review(root)

            self.assertFalse((root / "masks/capture").exists())
            self.assertIsNone(load_scan_metadata(root / "metadata").reconstruction_scope)
            self.assertEqual(load_mask_review(root)["state"], "awaiting_review")

    def test_review_rejects_invalid_indices_and_mismatched_preview_associations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._scan(Path(tmp), count=21)
            frames = load_scan_metadata(root / "metadata").frames
            result = generate_mask_proposals(root, frames)
            assert result is not None
            original = result.report_payload()
            invalid_indices = (
                [], [0, 1, 2, 3], [0, 1, 2, 3, 4, 5], [0, 0, 2, 3, 4],
                [4, 3, 2, 1, 0], [-1, 1, 2, 3, 4], [0, 1, 2, 3, 21],
                [False, 1, 2, 3, 4], [0.0, 1, 2, 3, 4], "0,1,2,3,4",
            )
            for indices in invalid_indices:
                with self.subTest(indices=indices):
                    payload = dict(original, review_indices=indices)
                    result.report_path.write_text(json.dumps(payload))
                    with self.assertRaises(MaskReviewError):
                        load_mask_review(root)
                    with self.assertRaises(ScanValidationError):
                        validate_scan_package(root)
                    with self.assertRaises(MaskReviewError):
                        approve_mask_review(root)
                    self.assertFalse((root / "masks/capture").exists())
            for masks in (
                original["review_masks"][::-1],
                original["review_masks"][:-1],
                ["../outside.png", *original["review_masks"][1:]],
            ):
                with self.subTest(masks=masks):
                    result.report_path.write_text(json.dumps(dict(original, review_masks=masks)))
                    with self.assertRaises(MaskReviewError):
                        load_mask_review(root)
                    with self.assertRaises(ScanValidationError):
                        validate_scan_package(root)

    def test_legacy_quartile_previews_remain_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._scan(Path(tmp), count=21)
            frames = load_scan_metadata(root / "metadata").frames
            # The prior generator emitted these exact indices; fixture it
            # through generation so real previews and full proposals exist.
            with patch("app.mask_generator._select_review_indices", return_value=(0, 5, 10, 15, 20)):
                result = generate_mask_proposals(root, frames)
            assert result is not None

            self.assertEqual(load_mask_review(root)["review_indices"], [0, 5, 10, 15, 20])
            approve_mask_review(root)
            self.assertEqual(validate_scan_package(root).capture_mask_count, 21)

    def test_validation_binds_review_report_to_metadata_and_exact_preview_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._scan(Path(tmp), count=21)
            frames = load_scan_metadata(root / "metadata").frames
            result = generate_mask_proposals(root, frames)
            assert result is not None
            payload = result.report_payload()
            index = result.review_indices[0]
            # A self-consistent report referring to a different source image
            # must still disagree with the trusted capture-frame ordering.
            payload["frames"][index]["image"] = "images/different.jpg"
            payload["review_masks"][0] = "masks/review/different.jpg.png"
            result.report_path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ScanValidationError, "capture metadata"):
                validate_scan_package(root)

            result.report_path.write_text(json.dumps(result.report_payload()))
            preview = root / result.review_masks[0]
            contents = preview.read_bytes()
            preview.unlink()
            with self.assertRaisesRegex(ScanValidationError, "association mismatch"):
                validate_scan_package(root)
            preview.write_bytes(contents)
            (preview.parent / "unexpected.png").write_bytes(contents)
            with self.assertRaisesRegex(ScanValidationError, "association mismatch"):
                validate_scan_package(root)

    def test_preview_failure_preserves_previous_generation_and_source_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._scan(Path(tmp), count=21)
            frames = load_scan_metadata(root / "metadata").frames
            generate_mask_proposals(root, frames)
            existing = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}

            with patch("app.mask_generator._write_review_preview", side_effect=OSError("preview failed")):
                with self.assertRaisesRegex(OSError, "preview failed"):
                    generate_mask_proposals(root, frames)

            self.assertEqual(existing, {path: path.read_bytes() for path in root.rglob("*") if path.is_file()})
            self.assertFalse(any(path.name.startswith(".") for path in (root / "masks").iterdir()))
            self.assertEqual(validate_scan_package(root).capture_mask_count, 0)

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
