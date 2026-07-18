from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.mask_generator import (  # noqa: E402
    PolygonInterpolationMaskGenerator,
    generate_mask_proposals,
)
from app.scan_metadata import FrameMetadata  # noqa: E402


class MaskGeneratorTests(unittest.TestCase):
    def test_generates_complete_interpolated_proposals_and_review_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames = self._frames(root, count=5)
            self._write_plan(root, frames, anchors=(0, 4))

            result = generate_mask_proposals(
                root,
                frames,
                generator=PolygonInterpolationMaskGenerator(resampled_point_count=16),
            )

            assert result is not None
            middle = Image.open(root / result.frames[2].mask)
            report = json.loads(result.report_path.read_text())
            review_exists = (root / result.review_masks[2]).is_file()

        self.assertEqual(len(result.frames), 5)
        self.assertEqual(result.frames[0].method, "authored")
        self.assertEqual(result.frames[2].method, "interpolated")
        self.assertEqual(result.frames[2].source_frame_ids, (0, 4))
        self.assertEqual(middle.getpixel((20, 50)), 255)
        self.assertEqual(middle.getpixel((5, 50)), 0)
        self.assertEqual(report["state"], "awaiting_review")
        self.assertEqual(report["review_indices"], [0, 1, 2, 3, 4])
        self.assertEqual(len(report["review_masks"]), 5)
        self.assertGreater(result.frames[2].safety_dilation_pixels, 0)
        self.assertTrue(review_exists)

    def test_single_anchor_propagates_both_directions_at_low_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames = self._frames(root, count=5)
            self._write_plan(root, frames, anchors=(2,))

            result = generate_mask_proposals(root, frames)

        assert result is not None
        self.assertEqual(result.frames[0].method, "boundary_hold")
        self.assertEqual(result.frames[4].method, "boundary_hold")
        self.assertLess(result.frames[0].confidence, 0.6)
        self.assertEqual(result.frames[2].confidence, 1.0)

    def test_incompatible_anchor_topology_uses_explicit_nearest_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames = self._frames(root, count=3)
            self._write_plan(root, frames, anchors=(0, 2), add_erase_to_last=True)

            result = generate_mask_proposals(root, frames)

        assert result is not None
        self.assertEqual(result.frames[1].method, "nearest_topology_fallback")
        self.assertEqual(result.frames[1].confidence, 0.25)

    def test_regeneration_atomically_replaces_owned_proposal_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames = self._frames(root, count=2)
            self._write_plan(root, frames, anchors=(0, 1))
            proposed = root / "masks" / "proposed"
            proposed.mkdir(parents=True)
            (proposed / "stale.png").write_bytes(b"stale")

            result = generate_mask_proposals(root, frames)

            names = sorted(path.name for path in result.output_dir.iterdir()) if result else []

        self.assertEqual(names, ["frame_000000.jpg.png", "frame_000001.jpg.png"])

    def test_abrupt_area_change_blocks_approval_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames = self._frames(root, count=2)
            self._write_plan(root, frames, anchors=(0, 1))
            path = root / "metadata" / "mask_authoring.json"
            payload = json.loads(path.read_text())
            payload["representative_frames"][0]["regions"][0]["points"] = [
                {"x": 0.45, "y": 0.45},
                {"x": 0.50, "y": 0.45},
                {"x": 0.50, "y": 0.50},
                {"x": 0.45, "y": 0.50},
            ]
            path.write_text(json.dumps(payload))

            result = generate_mask_proposals(root, frames)

        assert result is not None
        self.assertEqual(result.state, "needs_correction")
        self.assertIn(
            "abrupt_area_change",
            {issue["code"] for issue in result.blocking_issues},
        )

    @staticmethod
    def _frames(root: Path, *, count: int) -> tuple[FrameMetadata, ...]:
        (root / "metadata").mkdir(parents=True)
        (root / "images").mkdir()
        frames = tuple(
            FrameMetadata(index, f"images/frame_{index:06d}.jpg", float(index), (100, 100))
            for index in range(count)
        )
        for frame in frames:
            Image.new("RGB", frame.resolution, color=(90, 110, 130)).save(
                root / frame.image,
                format="JPEG",
            )
        return frames

    @staticmethod
    def _write_plan(
        root: Path,
        frames: tuple[FrameMetadata, ...],
        *,
        anchors: tuple[int, ...],
        add_erase_to_last: bool = False,
    ) -> None:
        representative_frames = []
        for anchor in anchors:
            offset = 0.1 + 0.2 * (anchor / max(len(frames) - 1, 1))
            regions = [{
                "operation": "keep",
                "points": [
                    {"x": offset, "y": 0.1}, {"x": offset + 0.4, "y": 0.1},
                    {"x": offset + 0.4, "y": 0.9}, {"x": offset, "y": 0.9},
                ],
            }]
            if add_erase_to_last and anchor == anchors[-1]:
                regions.append({
                    "operation": "erase",
                    "points": [
                        {"x": 0.4, "y": 0.4}, {"x": 0.6, "y": 0.4},
                        {"x": 0.5, "y": 0.6},
                    ],
                })
            representative_frames.append({
                "frame_id": frames[anchor].id,
                "image": frames[anchor].image,
                "regions": regions,
            })
        payload = {
            "schema_version": "1.0",
            "authoring_mode": "representative_frames",
            "coordinate_space": "normalized_capture_image",
            "mask_convention": "white_keep_black_exclude",
            "revision": 1,
            "representative_frames": representative_frames,
        }
        (root / "metadata" / "mask_authoring.json").write_text(json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
