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
    def test_review_samples_generated_frames_between_all_five_authored_samples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames = self._frames(root, count=21)
            anchors = (0, 5, 10, 15, 20)
            self._write_plan(root, frames, anchors=anchors)

            result = generate_mask_proposals(
                root,
                frames,
                generator=PolygonInterpolationMaskGenerator(resampled_point_count=16),
            )

            assert result is not None
            self.assertEqual(len(result.review_indices), 5)
            self.assertTrue(set(result.review_indices).isdisjoint(anchors))
            self.assertTrue(all(
                result.frames[index].method == "interpolated" for index in result.review_indices
            ))
            for lower, upper in zip(anchors, anchors[1:]):
                self.assertTrue(any(lower < index < upper for index in result.review_indices))

    def test_review_includes_low_confidence_fallback_not_only_authored_frames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames = self._frames(root, count=21)
            self._write_plan(root, frames, anchors=(0, 5, 10, 15, 20), add_erase_to_last=True)

            result = generate_mask_proposals(
                root, frames, generator=PolygonInterpolationMaskGenerator(resampled_point_count=16),
            )

            assert result is not None
            selected = [result.frames[index] for index in result.review_indices]
            self.assertTrue(any(frame.method == "nearest_topology_fallback" for frame in selected))
            self.assertEqual(min(frame.confidence for frame in selected), 0.25)
            self.assertEqual(len(selected), 5)

    def test_review_prioritizes_worst_area_and_centroid_changes(self) -> None:
        for change in ("area", "centroid"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                frames = self._frames(root, count=21)
                self._write_plan(root, frames, anchors=(0, 5, 6, 15, 20))
                plan_path = root / "metadata" / "mask_authoring.json"
                plan = json.loads(plan_path.read_text())
                # Adjacent authored keyframes isolate a change that the old
                # quartile review missed, without fabricating mask metrics.
                for frame in plan["representative_frames"]:
                    left = frame["frame_id"] < 6
                    x, y, width, height = (
                        (0.05 if left else 0.75, 0.3, 0.2, 0.4)
                        if change == "centroid" else
                        ((0.45, 0.45, 0.05, 0.05) if left else (0.1, 0.1, 0.8, 0.8))
                    )
                    frame["regions"][0]["points"] = [
                        {"x": x, "y": y}, {"x": x + width, "y": y},
                        {"x": x + width, "y": y + height}, {"x": x, "y": y + height},
                    ]
                plan_path.write_text(json.dumps(plan))

                result = generate_mask_proposals(
                    root, frames, generator=PolygonInterpolationMaskGenerator(resampled_point_count=16),
                )

                assert result is not None
                self.assertEqual(result.state, "needs_correction")
                self.assertIn(6, result.review_indices)
                self.assertTrue(any(
                    result.frames[index].method == "interpolated" for index in result.review_indices
                ))

    def test_short_and_fully_authored_scans_have_complete_bounded_unique_review_sets(self) -> None:
        for count in (1, 2, 3, 4, 5, 6, 8):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                frames = self._frames(root, count=count)
                self._write_plan(root, frames, anchors=tuple(range(count)))

                result = generate_mask_proposals(root, frames)

                assert result is not None
                self.assertEqual(len(result.review_indices), min(count, 5))
                self.assertEqual(result.review_indices, tuple(sorted(set(result.review_indices))))
                self.assertEqual(len(result.review_masks), min(count, 5))
                self.assertIn(0, result.review_indices)
                self.assertIn(count - 1, result.review_indices)
                if count <= 5:
                    self.assertEqual(result.review_indices, tuple(range(count)))

    def test_sampling_preserves_sources_complete_proposals_and_deterministic_preview_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames = self._frames(root, count=21)
            self._write_plan(root, frames, anchors=(0, 5, 10, 15, 20))
            sources = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            generator = PolygonInterpolationMaskGenerator(resampled_point_count=16)

            first = generate_mask_proposals(root, frames, generator=generator)
            assert first is not None
            proposals = {path.name: path.read_bytes() for path in first.output_dir.iterdir()}
            second = generate_mask_proposals(root, frames, generator=generator)
            assert second is not None

            self.assertEqual(first.report_payload(), second.report_payload())
            self.assertEqual(json.loads(second.report_path.read_text()), second.report_payload())
            self.assertEqual(len(proposals), len(frames))
            self.assertEqual(proposals, {path.name: path.read_bytes() for path in second.output_dir.iterdir()})
            self.assertTrue(all(path.read_bytes() == content for path, content in sources.items()))
            self.assertEqual(second.review_indices, tuple(sorted(set(second.review_indices))))
            expected_review_names = set()
            for index, preview in zip(second.review_indices, second.review_masks, strict=True):
                name = Path(frames[index].image).name + ".png"
                expected_review_names.add(name)
                self.assertEqual(preview, f"masks/review/{name}")
                with Image.open(root / preview) as overlay:
                    overlay.load()
                    self.assertEqual(overlay.mode, "RGB")
                    self.assertEqual(overlay.size, frames[index].resolution)
            self.assertEqual({path.name for path in (root / "masks/review").iterdir()}, expected_review_names)
            self.assertFalse((root / "masks/capture").exists())

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
