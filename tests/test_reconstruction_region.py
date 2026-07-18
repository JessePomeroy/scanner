from __future__ import annotations

import json
import math
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.reconstruction_region import (  # noqa: E402
    ReconstructionRegion,
    ReconstructionRegionError,
    ReconstructionRegionRevisionError,
    load_reconstruction_region,
    save_reconstruction_region,
)


def valid_payload() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "shape": "oriented_box",
        "coordinate_system": "colmap_reconstruction",
        "center": [1.5, -2.0, 3.25],
        "extents": [8.0, 6.0, 3.0],
        "orientation_xyzw": [0.0, 0.0, 0.0, 1.0],
        "source": "user_sparse_preview",
        "revision": 1,
    }


class ReconstructionRegionTests(unittest.TestCase):
    def test_round_trips_stable_contract(self) -> None:
        payload = valid_payload()

        region = ReconstructionRegion.from_dict(payload)

        self.assertEqual(region.as_dict(), payload)
        self.assertEqual(ReconstructionRegion.from_json(region.to_json()), region)
        self.assertNotIn("NaN", region.to_json())

    def test_accepts_supported_sources(self) -> None:
        for source in (
            "user_sparse_preview",
            "automatic",
            "arkit_alignment",
            "imported",
        ):
            with self.subTest(source=source):
                payload = valid_payload()
                payload["source"] = source
                self.assertEqual(ReconstructionRegion.from_dict(payload).source, source)

    def test_rejects_missing_and_unexpected_fields(self) -> None:
        missing = valid_payload()
        del missing["center"]
        with self.assertRaisesRegex(ReconstructionRegionError, "fields are missing: center"):
            ReconstructionRegion.from_dict(missing)

        unexpected = valid_payload()
        unexpected["centre"] = unexpected["center"]
        with self.assertRaisesRegex(ReconstructionRegionError, "Unexpected.*centre"):
            ReconstructionRegion.from_dict(unexpected)

    def test_rejects_unsupported_contract_identifiers(self) -> None:
        cases = {
            "schema_version": "2.0",
            "shape": "sphere",
            "coordinate_system": "arkit_world",
            "source": "unknown",
        }
        for field, invalid in cases.items():
            with self.subTest(field=field):
                payload = valid_payload()
                payload[field] = invalid
                with self.assertRaises(ReconstructionRegionError):
                    ReconstructionRegion.from_dict(payload)

    def test_rejects_invalid_vector_shapes_and_types(self) -> None:
        cases: list[tuple[str, object]] = [
            ("center", [0.0, 1.0]),
            ("extents", "8,6,3"),
            ("orientation_xyzw", [0.0, False, 0.0, 1.0]),
        ]
        for field, invalid in cases:
            with self.subTest(field=field):
                payload = valid_payload()
                payload[field] = invalid
                with self.assertRaises(ReconstructionRegionError):
                    ReconstructionRegion.from_dict(payload)

    def test_rejects_non_finite_values(self) -> None:
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                payload = valid_payload()
                payload["center"] = [value, 0.0, 0.0]
                with self.assertRaisesRegex(ReconstructionRegionError, "finite"):
                    ReconstructionRegion.from_dict(payload)

    def test_rejects_non_positive_extents(self) -> None:
        for value in (0.0, -1.0):
            with self.subTest(value=value):
                payload = valid_payload()
                payload["extents"] = [1.0, value, 1.0]
                with self.assertRaisesRegex(ReconstructionRegionError, "extents must be positive"):
                    ReconstructionRegion.from_dict(payload)

    def test_rejects_non_unit_orientation(self) -> None:
        payload = valid_payload()
        payload["orientation_xyzw"] = [0.0, 0.0, 0.0, 2.0]

        with self.assertRaisesRegex(ReconstructionRegionError, "unit quaternion"):
            ReconstructionRegion.from_dict(payload)

    def test_rejects_invalid_revision(self) -> None:
        for revision in (0, -1, True, 1.5):
            with self.subTest(revision=revision):
                payload = valid_payload()
                payload["revision"] = revision
                with self.assertRaises(ReconstructionRegionError):
                    ReconstructionRegion.from_dict(payload)

    def test_rejects_non_object_json(self) -> None:
        for raw in ("not-json", json.dumps([])):
            with self.subTest(raw=raw):
                with self.assertRaises(ReconstructionRegionError):
                    ReconstructionRegion.from_json(raw)

    def test_persists_first_next_and_idempotent_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_root = Path(tmp)
            (scan_root / "metadata").mkdir()
            first = ReconstructionRegion.from_dict(valid_payload())

            first_path = save_reconstruction_region(scan_root, first)
            same_path = save_reconstruction_region(scan_root, first)
            second_payload = valid_payload()
            second_payload["revision"] = 2
            second_payload["center"] = [2.0, 0.0, 0.0]
            second = ReconstructionRegion.from_dict(second_payload)
            second_path = save_reconstruction_region(scan_root, second)
            loaded = load_reconstruction_region(scan_root)

        self.assertEqual(first_path, same_path)
        self.assertEqual(second_path, first_path)
        self.assertEqual(loaded, second)

    def test_rejects_skipped_and_conflicting_revisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_root = Path(tmp)
            (scan_root / "metadata").mkdir()
            skipped_payload = valid_payload()
            skipped_payload["revision"] = 2
            with self.assertRaisesRegex(
                ReconstructionRegionRevisionError,
                "first.*must be 1",
            ):
                save_reconstruction_region(
                    scan_root,
                    ReconstructionRegion.from_dict(skipped_payload),
                )

            first = ReconstructionRegion.from_dict(valid_payload())
            save_reconstruction_region(scan_root, first)
            conflicting_payload = valid_payload()
            conflicting_payload["center"] = [9.0, 0.0, 0.0]
            with self.assertRaisesRegex(
                ReconstructionRegionRevisionError,
                "revision 1 already exists",
            ):
                save_reconstruction_region(
                    scan_root,
                    ReconstructionRegion.from_dict(conflicting_payload),
                )

            future_payload = valid_payload()
            future_payload["revision"] = 3
            with self.assertRaisesRegex(
                ReconstructionRegionRevisionError,
                "advance exactly one step",
            ):
                save_reconstruction_region(
                    scan_root,
                    ReconstructionRegion.from_dict(future_payload),
                )


if __name__ == "__main__":
    unittest.main()
