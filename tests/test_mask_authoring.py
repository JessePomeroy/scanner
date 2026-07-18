from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.mask_authoring import (  # noqa: E402
    MaskAuthoringError,
    load_mask_authoring_plan,
    representative_frame_indices,
)
from app.scan_metadata import FrameMetadata  # noqa: E402


class MaskAuthoringTests(unittest.TestCase):
    def test_loads_ordered_keep_and_erase_regions_bound_to_exact_frame(self) -> None:
        frames = (FrameMetadata(7, "images/frame.jpg", 1.0, (100, 200)),)
        payload = self._payload()
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp)
            (metadata / "mask_authoring.json").write_text(json.dumps(payload))
            plan = load_mask_authoring_plan(metadata, frames)

        assert plan is not None
        self.assertEqual(plan.revision, 1)
        self.assertEqual([region.operation for region in plan.representative_frames[0].regions], [
            "keep", "erase",
        ])
        self.assertEqual(plan.as_dict(), payload)

    def test_rejects_frame_id_image_pair_not_in_frames_metadata(self) -> None:
        frames = (FrameMetadata(7, "images/different.jpg", 1.0, (100, 200)),)
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp)
            (metadata / "mask_authoring.json").write_text(json.dumps(self._payload()))
            with self.assertRaisesRegex(MaskAuthoringError, "association"):
                load_mask_authoring_plan(metadata, frames)

    def test_rejects_erase_only_and_degenerate_authoring(self) -> None:
        payload = self._payload()
        payload["representative_frames"][0]["regions"] = [
            {"operation": "erase", "points": self._points()}
        ]
        frames = (FrameMetadata(7, "images/frame.jpg", 1.0, (100, 200)),)
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp)
            path = metadata / "mask_authoring.json"
            path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(MaskAuthoringError, "keep region"):
                load_mask_authoring_plan(metadata, frames)
            payload["representative_frames"][0]["regions"][0]["operation"] = "keep"
            payload["representative_frames"][0]["regions"][0]["points"] = [
                {"x": 0.1, "y": 0.1}, {"x": 0.2, "y": 0.2}, {"x": 0.3, "y": 0.3}
            ]
            path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(MaskAuthoringError, "degenerate"):
                load_mask_authoring_plan(metadata, frames)

    def test_rejects_unknown_fields_nonfinite_values_and_symlinks(self) -> None:
        frames = (FrameMetadata(7, "images/frame.jpg", 1.0, (100, 200)),)
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp)
            path = metadata / "mask_authoring.json"
            payload = self._payload()
            payload["unexpected"] = True
            path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(MaskAuthoringError, "fields"):
                load_mask_authoring_plan(metadata, frames)
            path.write_text(json.dumps(self._payload()).replace("0.1", "NaN", 1))
            with self.assertRaisesRegex(MaskAuthoringError, "non-finite"):
                load_mask_authoring_plan(metadata, frames)
            path.unlink()
            external = metadata / "external.json"
            external.write_text(json.dumps(self._payload()))
            path.symlink_to(external)
            with self.assertRaisesRegex(MaskAuthoringError, "unsafe"):
                load_mask_authoring_plan(metadata, frames)

    def test_selects_stable_five_sample_review_frames(self) -> None:
        self.assertEqual(representative_frame_indices(1), (0,))
        self.assertEqual(representative_frame_indices(9), (0, 2, 4, 6, 8))
        self.assertEqual(representative_frame_indices(10), (0, 2, 4, 7, 9))

    @staticmethod
    def _points() -> list[dict[str, float]]:
        return [
            {"x": 0.1, "y": 0.1}, {"x": 0.9, "y": 0.1}, {"x": 0.9, "y": 0.9},
        ]

    @classmethod
    def _payload(cls) -> dict:
        return {
            "schema_version": "1.0",
            "authoring_mode": "representative_frames",
            "coordinate_space": "normalized_capture_image",
            "mask_convention": "white_keep_black_exclude",
            "revision": 1,
            "representative_frames": [{
                "frame_id": 7,
                "image": "images/frame.jpg",
                "regions": [
                    {"operation": "keep", "points": cls._points()},
                    {"operation": "erase", "points": [
                        {"x": 0.4, "y": 0.4}, {"x": 0.6, "y": 0.4}, {"x": 0.5, "y": 0.6},
                    ]},
                ],
            }],
        }


if __name__ == "__main__":
    unittest.main()
