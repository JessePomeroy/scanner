from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.mask_profiles import mask_stage_profile  # noqa: E402


class MaskProfileTests(unittest.TestCase):
    def test_scene_profile_preserves_full_image_alignment(self) -> None:
        profile = mask_stage_profile("scene_geometry")
        report = profile.as_dict(masks_available=True)

        self.assertFalse(report["stages"]["colmap_features"])
        self.assertTrue(report["stages"]["colmap_stereo_fusion"])
        self.assertTrue(report["stages"]["openmvs_densification"])
        self.assertTrue(report["stages"]["openmvs_texturing"])

    def test_object_profile_can_constrain_alignment(self) -> None:
        profile = mask_stage_profile("object_foreground")
        report = profile.as_dict(masks_available=True)

        self.assertTrue(all(report["stages"].values()))

    def test_profile_reports_no_consumers_without_reviewed_masks(self) -> None:
        profile = mask_stage_profile("scene_geometry")
        report = profile.as_dict(masks_available=False)

        self.assertFalse(any(report["stages"].values()))


if __name__ == "__main__":
    unittest.main()
