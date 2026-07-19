from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.mask_alignment import (  # noqa: E402
    MaskAlignmentCheckpointError,
    load_mask_alignment_checkpoint,
    publish_mask_alignment_checkpoint,
)


class MaskAlignmentCheckpointTests(unittest.TestCase):
    def test_round_trips_object_alignment_intent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = publish_mask_alignment_checkpoint(
                root,
                run_dense=True,
                run_openmvs=True,
                scope_mode="auto_roi",
                use_masks=False,
                review_scope=True,
                mask_profile="object_foreground",
            )
            checkpoint = load_mask_alignment_checkpoint(root)

        self.assertEqual(path.name, "mask_alignment_checkpoint.json")
        self.assertTrue(checkpoint.run_dense)
        self.assertTrue(checkpoint.run_openmvs)
        self.assertTrue(checkpoint.review_scope)
        self.assertEqual(checkpoint.mask_profile, "object_foreground")

    def test_rejects_scene_profile_and_tampered_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(MaskAlignmentCheckpointError):
                publish_mask_alignment_checkpoint(
                    root,
                    run_dense=True,
                    run_openmvs=True,
                    scope_mode="auto_roi",
                    use_masks=False,
                    review_scope=True,
                    mask_profile="scene_geometry",
                )
            path = publish_mask_alignment_checkpoint(
                root,
                run_dense=True,
                run_openmvs=True,
                scope_mode="auto_roi",
                use_masks=False,
                review_scope=True,
                mask_profile="object_foreground",
            )
            payload = json.loads(path.read_text())
            payload["continuation"]["mask_profile"] = "scene_geometry"
            path.write_text(json.dumps(payload))

            with self.assertRaises(MaskAlignmentCheckpointError):
                load_mask_alignment_checkpoint(root)


if __name__ == "__main__":
    unittest.main()
